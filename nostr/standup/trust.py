# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which key a submission is encrypted to (editorial contract 8.1).

A publication's editorial team ("Redaktion") reads submissions with an
epoch key whose public half the publication signs in a key event (kind
30078, ``d`` = ``standup:redaktion-key:<site d>``). The server hands
that event out, and so do the publication's relays. Neither is trusted
on its own word: before anything is wrapped to the key, the event must
pass every check of 8.1 (:func:`verify_key_event`), and its epoch must
not be older than the highest this app has seen for the publication
(:class:`EpochStore`, kept in the app's settings), so a server cannot
hand back a key from before a member left. Of all valid events, from
the server and from the relays, the newest counts (highest epoch, then
the latest ``created_at``; :func:`newest_key`).

When nothing passes, submitting stops with the contract's words
(:data:`KEY_NOT_CONFIRMED`, in German "Der Schlüssel der Redaktion ist
nicht bestätigt. Einreichen ist gerade nicht möglich."). Why exactly it
failed stays in :attr:`KeyNotConfirmed.reason`, for logs and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from PySide6.QtCore import QObject

from i18n import N_

from .. import events as nostr_events
from .. import queries
from . import formats
from .answers import Listing, SiteSettings
from .client import StandupApi
from .errors import StandupError
from .servers import usable_server_url
from .formats import FormatError

KEY_NOT_CONFIRMED = N_(
    "The editorial team's key is not confirmed. Submitting is not possible right now.")

# The settings key under which the highest epoch per publication is kept.
SETTINGS_KEY = "standup_redaktion_epochs"
# A key event's content is a few hundred bytes; a checklist makes it
# longer, never this long.
MAX_KEY_EVENT_CONTENT = 64 * 1024


class KeyNotConfirmed(ValueError):
    """The publication's key cannot be used. ``reason`` is why, in
    English, never shown; the words to show are :data:`KEY_NOT_CONFIRMED`
    (translate with ``_()``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def words(self) -> str:
        return KEY_NOT_CONFIRMED


@dataclass(frozen=True)
class RedaktionKey:
    """A verified key event: whom to wrap to, and the rules the
    publication signed."""

    address: str
    publication_pubkey: str
    site_d: str
    epoch: int
    pubkey: str                 # P_epoch
    server: str                 # usable submission server, "" when none
    owner: str
    name: str
    rules: Mapping[str, Any]
    event_id: str
    created_at: int
    event: Mapping[str, Any] = field(repr=False)


def verify_key_event(event: Any, address: str, *, highest_seen: int = 0,
                     local_http: Optional[bool] = None) -> RedaktionKey:
    """``event`` as the key of the publication at ``address``, with every
    check of 8.1, or KeyNotConfirmed: kind 30078; a valid id and
    signature; signed by the publication's key; one ``d`` tag, the right
    one; ``content.publication`` is the address; ``content.epoch`` an
    integer no lower than ``highest_seen``; ``content.pubkey`` a valid
    x-only key. The content is read strictly (no key twice), and its
    server must be an address this app may send to."""
    try:
        pub_pubkey, site_d = formats.parse_address(address)
    except FormatError as exc:
        raise KeyNotConfirmed(f"the address: {exc}") from None
    if not isinstance(event, dict) or event.get("kind") != formats.APP_DATA_KIND:
        raise KeyNotConfirmed("not a kind 30078 event")
    if not nostr_events.is_verified_event(event, kind=formats.APP_DATA_KIND):
        raise KeyNotConfirmed("the signature does not verify")
    if event["pubkey"] != pub_pubkey:
        raise KeyNotConfirmed("signed by another key than the publication's")
    if formats.tag_values(event, "d") != [formats.key_event_d(site_d)]:
        raise KeyNotConfirmed("the d tag is not this publication's key event")
    try:
        content = formats.strict_json(event["content"], limit=MAX_KEY_EVENT_CONTENT)
    except FormatError as exc:
        raise KeyNotConfirmed(f"the content: {exc}") from None
    if not isinstance(content, dict) or content.get("v") != formats.CONTRACT_VERSION \
            or not formats.is_int(content.get("v")):
        raise KeyNotConfirmed("the content is of another version")
    if content.get("publication") != address:
        raise KeyNotConfirmed("the content names another publication")
    epoch = content.get("epoch")
    if not formats.is_int(epoch):
        raise KeyNotConfirmed("the epoch is not a whole number")
    if epoch < highest_seen:
        raise KeyNotConfirmed(f"epoch {epoch} is older than {highest_seen}, seen before")
    if not formats.is_pubkey(content.get("pubkey")):
        raise KeyNotConfirmed("the key is not a valid x-only key")
    rules = content.get("rules")
    if not isinstance(rules, dict):
        raise KeyNotConfirmed("the rules are missing")
    owner = content.get("owner")
    if not formats.is_hex64(owner):
        raise KeyNotConfirmed("the owner is missing")
    name = content.get("name")
    server = usable_server_url(content.get("server"), local_http=local_http)
    if not server:
        raise KeyNotConfirmed("the key names no server origin this app may send to")
    return RedaktionKey(address=address, publication_pubkey=pub_pubkey, site_d=site_d,
                        epoch=epoch, pubkey=content["pubkey"], server=server, owner=owner,
                        name=name if isinstance(name, str) else "", rules=dict(rules),
                        event_id=event["id"], created_at=event["created_at"],
                        event=dict(event))


# --------------------------------------------------------------------------- #
# The highest epoch seen                                                       #
# --------------------------------------------------------------------------- #

class EpochStore:
    """The highest epoch seen per publication, in the app's settings
    (``settings.json``, key :data:`SETTINGS_KEY`). Only ever raised.

    Publication addresses and epochs are public (the key events are on
    relays), so they may live in the plain settings file; nothing secret
    does. ``load`` and ``save`` are seams."""

    def __init__(self, *, load: Optional[Callable[[], dict]] = None,
                 save: Optional[Callable[[str, Any], None]] = None) -> None:
        if load is None or save is None:
            import settings as app_settings
            load = load or app_settings.load_settings
            save = save or app_settings.save_setting
        self._load = load
        self._save = save

    def _all(self) -> Dict[str, int]:
        stored = (self._load() or {}).get(SETTINGS_KEY)
        if not isinstance(stored, dict):
            return {}
        return {k: v for k, v in stored.items()
                if isinstance(k, str) and formats.is_int(v)}

    def highest(self, address: str) -> int:
        return self._all().get(address, 0)

    def note(self, address: str, epoch: int) -> None:
        """Remember ``epoch`` when it is higher than what was seen."""
        known = self._all()
        if formats.is_int(epoch) and epoch > known.get(address, 0):
            known[address] = epoch
            self._save(SETTINGS_KEY, known)


def newest_key(candidates: Iterable[Any], address: str, store: EpochStore, *,
               local_http: Optional[bool] = None) -> RedaktionKey:
    """The newest valid key event among ``candidates`` (from the server
    and from relays): highest epoch, then latest ``created_at``, then the
    lower id, so every app picks the same one. Its epoch is remembered.
    Raises KeyNotConfirmed when none passes, with the first reason."""
    highest = store.highest(address)
    valid: List[RedaktionKey] = []
    reasons: List[str] = []
    for event in candidates:
        if event is None:
            continue
        try:
            valid.append(verify_key_event(event, address, highest_seen=highest,
                                          local_http=local_http))
        except KeyNotConfirmed as refusal:
            reasons.append(refusal.reason)
    if not valid:
        raise KeyNotConfirmed(reasons[0] if reasons else "no key event was found")
    best = min(valid, key=lambda k: (-k.epoch, -k.created_at, k.event_id))
    store.note(address, best.epoch)
    return best


# --------------------------------------------------------------------------- #
# Asking relays                                                                #
# --------------------------------------------------------------------------- #

# ``relay_lookup(publication_pubkey, d_tag, on_done(events))``: the key
# events the publication's relays have. Exactly one call of on_done.
RelayLookup = Callable[[str, str, Callable[[Sequence[dict]], None]], None]


def relay_key_lookup(pool, directory, *, timeout_ms: int = 8_000) -> RelayLookup:
    """A :data:`RelayLookup` over the app's relay pool: the publication's
    outbox relays (``RelayDirectory.outbox_of``), one query, only validly
    signed events by that key (``queries.fetch_events``)."""

    def lookup(pubkey: str, d_tag: str, on_done: Callable[[Sequence[dict]], None]) -> None:
        def got_relays(relays: List[str]) -> None:
            if not relays:
                on_done([])
                return
            queries.fetch_events(pool, relays,
                                 [{"kinds": [formats.APP_DATA_KIND], "authors": [pubkey],
                                   "#d": [d_tag]}],
                                 lambda fetched: on_done(list(fetched.events)),
                                 accept=lambda e: e.get("pubkey") == pubkey
                                 and e.get("kind") == formats.APP_DATA_KIND,
                                 timeout_ms=timeout_ms)

        directory.outbox_of(pubkey, got_relays)

    return lookup


def key_for_server(key: RedaktionKey, server: str) -> RedaktionKey:
    """``key`` when it names ``server``, the server a submission would
    go to (or that listed the publication), else KeyNotConfirmed (8.1,
    feat/redaktion 0820f0d: submit only to the key's server). A receipt
    only mirrors the body's hashes, so any server could mint a matching
    one; only the signed key event says which server is the
    publication's."""
    if not server or key.server != server:
        raise KeyNotConfirmed("the key names another server than this one")
    return key


@dataclass(frozen=True)
class Destination:
    """A publication one can submit to: as listed, with its verified key
    (``key``, naming the server that listed it), or why it cannot be
    used (``problem``, never shown)."""

    address: str
    name: str
    picture: Optional[str]
    key: Optional[RedaktionKey]
    problem: str = ""

    @property
    def usable(self) -> bool:
        return self.key is not None and bool(self.key.rules.get("open", False))


class Destinations(QObject):
    """The publications open for submissions, each with a verified key.

    :meth:`find` asks the server for its list
    (``GET /api/submissions/destinations``) and, for each publication,
    the publication's relays for its key event, then keeps the newest
    valid one of either source (8.1). The name shown is the signed one
    when the key event has one."""

    def __init__(self, api: StandupApi, relay_lookup: RelayLookup, store: EpochStore, *,
                 local_http: Optional[bool] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._api = api
        self._relay_lookup = relay_lookup
        self._store = store
        self._local_http = local_http
        self._generation = 0

    def stop(self) -> None:
        """Answers still on their way change nothing. The client is
        shared (a submission may be on its way), so it is not cancelled."""
        self._generation += 1

    def find(self, on_done: Callable[[List[Destination]], None], on_failure) -> None:
        generation = self._generation

        def listed(items: Sequence[Listing]) -> None:
            if generation != self._generation:
                return
            results: Dict[int, Destination] = {}
            if not items:
                on_done([])
                return

            def one(index: int, item: Listing) -> None:
                def found(events: Sequence[dict]) -> None:
                    if generation != self._generation or index in results:
                        return
                    results[index] = self._destination(item, [item.key_event, *events])
                    if len(results) == len(items):
                        on_done([results[i] for i in range(len(items))])

                pubkey, site_d = formats.parse_address(item.address)
                self._relay_lookup(pubkey, formats.key_event_d(site_d), found)

            for index, item in enumerate(items):
                one(index, item)

        def failed(error: StandupError) -> None:
            if generation == self._generation:
                on_failure(error)

        self._api.destinations(listed, failed)

    def _destination(self, item: Listing, candidates: Sequence[Any]) -> Destination:
        try:
            key = key_for_server(newest_key(candidates, item.address, self._store,
                                            local_http=self._local_http), self._api.server)
        except KeyNotConfirmed as refusal:
            return Destination(address=item.address, name=item.name, picture=item.picture,
                               key=None, problem=refusal.reason)
        return Destination(address=item.address, name=key.name or item.name,
                           picture=item.picture, key=key)


def confirm_key(api: StandupApi, address: str, relay_lookup: RelayLookup, store: EpochStore,
                on_done: Callable[[RedaktionKey], None],
                on_refused: Callable[[KeyNotConfirmed], None], *,
                local_http: Optional[bool] = None) -> None:
    """The key to submit to, checked right before submitting: the
    server's current key event (``GET .../settings``) and the relays',
    the newest valid one, which must name ``api``'s server
    (:func:`key_for_server`). When the server cannot be asked, the
    relays' alone count. ``on_refused`` with KeyNotConfirmed when none
    passes, its reason naming the server's failure when there was one."""
    try:
        pubkey, site_d = formats.parse_address(address)
    except FormatError as exc:
        on_refused(KeyNotConfirmed(f"the address: {exc}"))
        return
    state: Dict[str, Any] = {}

    def maybe_done() -> None:
        if "server" not in state or "relays" not in state or state.get("done"):
            return
        state["done"] = True
        try:
            key = key_for_server(newest_key([state["server"], *state["relays"]], address,
                                            store, local_http=local_http), api.server)
        except KeyNotConfirmed as refusal:
            error = state.get("server_error")
            on_refused(refusal if error is None else KeyNotConfirmed(
                f"{refusal.reason}; the server could not be asked ({error.code})"))
            return
        on_done(key)

    def from_server(settings: SiteSettings) -> None:
        state["server"] = settings.key_event
        maybe_done()

    def server_failed(error: StandupError) -> None:
        state["server"] = None
        state["server_error"] = error
        maybe_done()

    def from_relays(events: Sequence[dict]) -> None:
        state["relays"] = list(events)
        maybe_done()

    api.site_settings(address, from_server, server_failed)
    relay_lookup(pubkey, formats.key_event_d(site_d), from_relays)


__all__ = [
    "Destination", "Destinations", "EpochStore", "KEY_NOT_CONFIRMED", "KeyNotConfirmed",
    "RedaktionKey", "RelayLookup", "SETTINGS_KEY", "confirm_key", "key_for_server", "newest_key",
    "relay_key_lookup", "verify_key_event",
]
