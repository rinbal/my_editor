# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Taking back what was published: deletion requests (NIP-09).

A deletion request is a kind 5 event that names events its author wants
removed. ``e`` tags name one event each, by id. ``a`` tags name an event
that newer versions replace (an article, a draft) by its address,
``<kind>:<pubkey>:<d>``, and remove every version of it up to the time of
the request, so a version published later stays. ``k`` tags say the kinds.

Relays that honour a request stop handing those events out. People and
apps that saved a copy may still have it, which is why the app says it
asks relays to remove something rather than that it is gone.

One home for each rule:

    address          the address of a replaceable or addressable event
    build_deletion   an unsigned request naming events
    read_deletion    what a validly signed request by an author asks to
                     remove (a DeletionRequest); anything else asks nothing
    DeletionRequest.covers
                     whether a request covers an event: only the author's
                     own events, and an address only up to its time

:class:`DeletionJob` takes one of the account's published events back
through the publish pipeline every publish uses: the request is signed
through the session pool, checked, and sent to every relay that may keep
the event, relay by relay.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject

from i18n import _

from . import events, refs
from .outbox.policy import created_at_of
from .publish_pipeline import PublishPipeline

DELETION_KIND = 5

_HEX64 = re.compile(r"[0-9a-f]{64}")     # an event id, a public key


# --------------------------------------------------------------------------- #
# Addresses                                                                   #
# --------------------------------------------------------------------------- #

def is_replaceable(kind: int) -> bool:
    """NIP-01: one event per author and kind (a profile, a relay list)."""
    return kind in (0, 3) or 10_000 <= kind < 20_000


def is_addressable(kind: int) -> bool:
    """NIP-01: one event per author, kind and ``d`` (an article, a draft)."""
    return 30_000 <= kind < 40_000


def address(kind: int, pubkey_hex: str, identifier: str = "") -> str:
    """NIP-01 address: ``<kind>:<pubkey>:<d>`` (``d`` empty for replaceable kinds)."""
    return f"{int(kind)}:{pubkey_hex.lower()}:{identifier}"


def address_of(event: dict) -> Optional[str]:
    """The address of a replaceable or addressable event, None for any other."""
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    if isinstance(kind, bool) or not isinstance(kind, int):
        return None
    pubkey = str(event.get("pubkey", "")).lower()
    if is_replaceable(kind):
        return address(kind, pubkey)
    if is_addressable(kind):
        return address(kind, pubkey, _d_of(event))
    return None


def _d_of(event: dict) -> str:
    for tag in event.get("tags", []) or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d":
            return str(tag[1])
    return ""


_KIND = re.compile(r"[0-9]+")   # ASCII only: str.isdigit() also takes "²", which int() refuses


def _normal_address(value: str) -> Optional[str]:
    """An ``a`` tag's address with kind and pubkey written one way, or None."""
    parts = value.split(":", 2)
    if len(parts) != 3:
        return None
    kind, pubkey, identifier = parts
    if not _KIND.fullmatch(kind) or not _HEX64.fullmatch(pubkey.lower()):
        return None
    return address(int(kind), pubkey, identifier)


# --------------------------------------------------------------------------- #
# Requests                                                                    #
# --------------------------------------------------------------------------- #

def build_deletion(pubkey_hex: str, *, addresses: Iterable[str] = (),
                   event_ids: Iterable[str] = (), kinds: Iterable[int] = (),
                   created_at: Optional[int] = None) -> dict:
    """An unsigned request to delete the events at ``addresses`` and with
    ``event_ids``, of ``kinds``. An address holding a Nostr secret key (a
    slug made from a title with one) is never named: the request would
    send the key again, so such an event is named by its id alone. Raises
    ValueError when it names nothing."""
    named_addresses = [address for address in dict.fromkeys(addresses)
                       if not refs.secret_keys_in(address)]
    named_ids = list(dict.fromkeys(event_ids))
    if not named_addresses and not named_ids:
        raise ValueError("a deletion request names at least one event")
    tags = [["a", value] for value in named_addresses]
    tags += [["e", value] for value in named_ids]
    tags += [["k", str(int(kind))] for kind in dict.fromkeys(kinds)]
    return events.build_event(kind=DELETION_KIND, content="", tags=tags,
                              pubkey_hex=pubkey_hex, created_at=created_at)


@dataclass(frozen=True)
class DeletionRequest:
    """What one validly signed deletion request asks to remove."""

    id: str
    pubkey: str
    created_at: int
    event_ids: FrozenSet[str] = frozenset()
    addresses: FrozenSet[str] = frozenset()     # "<kind>:<pubkey>:<d>"

    def covers(self, event: dict) -> bool:
        """Whether this request removes ``event`` (NIP-09): one of the
        requester's own events named by id, or a version of a named
        address no newer than the request."""
        if not isinstance(event, dict) or str(event.get("pubkey", "")).lower() != self.pubkey:
            return False
        if str(event.get("id", "")).lower() in self.event_ids:
            return True
        where = address_of(event)
        if where is None or where not in self.addresses:
            return False
        created = created_at_of(event)
        return created is not None and created <= self.created_at

    def identifiers(self, kind: int) -> List[str]:
        """The ``d`` of every address of ``kind`` this request names."""
        prefix = address(kind, self.pubkey)
        return sorted(value[len(prefix):] for value in self.addresses
                      if value.startswith(prefix))


def read_deletion(event: dict, author: str) -> Optional[DeletionRequest]:
    """What ``event`` asks to remove, when it is a deletion request signed
    by ``author``; None for anything else. A relay cannot delete anyone's
    events by inventing a request: only the author's signature counts."""
    author = (author or "").lower()
    if not isinstance(event, dict) or event.get("kind") != DELETION_KIND:
        return None
    if str(event.get("pubkey", "")).lower() != author:
        return None
    created = created_at_of(event)
    if created is None or not events.verify_event(event):
        return None
    ids, addresses = set(), set()
    for tag in event.get("tags", []):
        if not isinstance(tag, list) or len(tag) < 2 or not isinstance(tag[1], str):
            continue
        if tag[0] == "e":
            ids.add(tag[1].lower())
        elif tag[0] == "a":
            normalized = _normal_address(tag[1])
            if normalized is not None:
                addresses.add(normalized)
    return DeletionRequest(id=str(event["id"]), pubkey=author, created_at=created,
                           event_ids=frozenset(ids), addresses=frozenset(addresses))


# --------------------------------------------------------------------------- #
# DeletionJob: sign a request, send it where the event may be kept            #
# --------------------------------------------------------------------------- #

class DeletionJob(PublishPipeline):
    """Ask relays to delete one of the account's published events.

    The request names the event by id, one with an address (an article)
    also by its address, and says its kind. It goes the way any publish
    goes (publish_pipeline.PublishPipeline: the routing and the signature
    at the same time, the signed request checked to be exactly the one
    asked for): to every relay the event was found on, kept first, and
    wherever the account publishes, the relays of the people the event
    mentions among them, so each relay that may keep a copy is asked.

    Signals: those of the pipeline (status_changed, relay_result,
    first_accept, completed, failed). ``request`` is the signed request
    once the signer has signed it, and ``send_again()`` sends it once
    more without asking the signer again.
    """

    def __init__(self, *, relay_pool, relay_directory, session_pool, profile,
                 event: dict, found_on: Sequence[str] = (),
                 mentioned: Sequence[Tuple[str, str]] = (), entitled_relays=(),
                 clock: Callable[[], float] = time.time,
                 parent: Optional[QObject] = None) -> None:
        if not isinstance(event, dict):
            raise ValueError("not a published event")
        kind = event.get("kind")
        if (not _HEX64.fullmatch(str(event.get("id", ""))) or isinstance(kind, bool)
                or not isinstance(kind, int)):
            raise ValueError("not a published event")
        author = str(profile.user_pubkey).lower()
        if str(event.get("pubkey", "")).lower() != author:
            raise ValueError("only the account's own events can be deleted")
        where = address_of(event)
        request = build_deletion(author, addresses=[where] if where else [],
                                 event_ids=[event["id"]], kinds=[kind],
                                 created_at=int(clock()))
        super().__init__(relay_pool=relay_pool, relay_directory=relay_directory,
                         session_pool=session_pool, profile=profile, unsigned_event=request,
                         entitled_relays=entitled_relays, extra_relays=found_on,
                         mentioned=mentioned, parent=parent)

    @property
    def request(self) -> Optional[dict]:
        """The signed request, once the signer has signed it."""
        return self.signed_event

    @property
    def targets(self) -> List[str]:
        """Where the request goes (known once the routing is in)."""
        return list(self._targets or ())

    def _waiting_words(self) -> str:
        return _("Waiting for your signer app. Approve the deletion there…")

    def _sending_words(self, count: int) -> str:
        return _("Asking for it to be deleted…")
