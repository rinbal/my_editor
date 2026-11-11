# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""People who left (editorial contract 10, "People who left"; M-4 item 2).

The member list holds today's members only. An action signed by someone
who has left or was removed since verifies from what the owner and the
publication key signed, never from the server's word: the read of a
submission serves a ``Former`` for each such signer, with the stored
grants that gave them the Redaktion's key and the latest key event that
named them owner.

:func:`standing` is the one check, as ``formerStanding`` in the
contract's ``docs/editorial-fixtures/crosscheck.reference.mjs`` has it
(vector ``former_member``): a counting grant or owner key event at the
action's epoch e or later makes the person a member then (the lowest
such epoch is g); they may decide when the owner key event reaches e, or
when their newest counting invitation at or below g has the role
``redaktion``. :class:`Formers` keeps the Formers of one submission,
joined by pubkey across reads, checks each grant once, and picks the
epoch an action names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .. import events as nostr_events
from . import formats
from .formats import FormatError
from .readers import read_list, read_object, read_text

# At most 8 people in one answer (contract 10); a Former's grants prove
# the epochs its person's actions name, two at most for each.
MAX_FORMERS = 8
MAX_GRANTS = 64
_MAX_GRANT_BODY = 1_048_576
ROLES = ("redaktion", "curator")
# The widest span of epochs one action's unread revision may lie in.
_MOST_EPOCHS = 64
# Actions a member may take, which name no epoch (contract 10); the
# decisions and a correction need one who may decide; no other action
# (``published``, ``placed``) takes this path.
MEMBER_ACTIONS = ("taken", "handed_over", "handed_back", "reupload_requested")


@dataclass(frozen=True)
class Grant:
    """One stored signed request that gave the person a key (12.3, 12.7):
    the NIP-98 ``event``, its exact ``body``, and for an invitation the
    key event version it was made under."""

    event: Mapping[str, Any]
    body: str
    key_event: Optional[Mapping[str, Any]]


@dataclass(frozen=True)
class Former:
    pubkey: str
    grants: Tuple[Grant, ...]
    owner_key_event: Optional[Mapping[str, Any]]


@dataclass(frozen=True)
class Standing:
    member: bool = False
    decides: bool = False


def parse_formers(value: Any) -> Tuple[Former, ...]:
    """``former`` of ``GET /api/submissions/<sid>`` (missing: none)."""
    out = []
    for item in read_list(value if value is not None else [], "the former members",
                          limit=MAX_FORMERS):
        data = read_object(item, "a former member")
        pubkey = data.get("pubkey")
        formats.need(formats.is_pubkey(pubkey), "a former member's key is malformed")
        grants = tuple(_grant_of(g) for g in read_list(data.get("grants") or [],
                                                         "a former member's grants",
                                                         limit=MAX_GRANTS))
        owner = data.get("owner_key_event")
        out.append(Former(pubkey=pubkey, grants=grants, owner_key_event=(
            dict(read_object(owner, "an owner key event")) if owner is not None else None)))
    return tuple(out)


def _grant_of(value: Any) -> Grant:
    data = read_object(value, "a grant")
    key_event = data.get("key_event")
    return Grant(event=dict(read_object(data.get("event"), "a grant's event")),
                 body=read_text(data.get("body"), "a grant's body", limit=_MAX_GRANT_BODY),
                 key_event=dict(read_object(key_event, "a grant's key event"))
                 if key_event is not None else None)


def _key(event: Any, address: str):
    """A key event of ``address`` as 8.1 has it, of any epoch from 1
    (``trust.RedaktionKey``), or None."""
    from . import trust                 # trust reads answers, which read Formers
    try:
        return trust.verify_key_event(event, address, highest_seen=1)
    except trust.KeyNotConfirmed:
        return None


def _proof(grant: Grant, person: str, address: str) -> Optional[Tuple[int, Optional[str], int]]:
    """``(epoch, role or None, created_at)`` when ``grant`` counts for
    ``person`` (contract 10, "A grant counts"), else None."""
    event, body = grant.event, grant.body
    if not nostr_events.is_verified_event(dict(event), kind=27235):
        return None
    urls = formats.tag_values(dict(event), "u")
    if len(urls) != 1 or formats.tag_values(dict(event), "method") != ["POST"] \
            or formats.tag_values(dict(event), "payload") != [formats.sha256_hex(body)]:
        return None
    try:
        said = formats.strict_json(body, limit=_MAX_GRANT_BODY)
        pub, site_d = formats.parse_address(address)
    except FormatError:
        return None
    if not isinstance(said, dict):
        return None
    route = formats.route_key(pub, site_d)

    def bound(key_event: Any, path: str):
        key = _key(key_event, address)
        return key if key is not None and event.get("pubkey") == key.owner \
            and urls[0] == f"{key.server}/api/sites/{route}/{path}" else None

    key = bound(grant.key_event, "members")
    if key is not None:
        wraps = said.get("wraps")
        named = said.get("pubkey") == person and isinstance(wraps, list) and any(
            isinstance(w, dict) and formats.is_int(w.get("epoch")) and w["epoch"] == key.epoch
            for w in wraps)
        return (key.epoch, said["role"], event["created_at"]) \
            if named and said.get("role") in ROLES else None
    for path, new_epoch in (("epochs", said), ("members/remove", said.get("epoch"))):
        key = bound(new_epoch.get("key_event"), path) if isinstance(new_epoch, dict) else None
        if key is not None:
            wraps = new_epoch.get("wraps")
            return (key.epoch, None, event["created_at"]) \
                if isinstance(wraps, dict) and person in wraps else None
    return None


def _owner_epoch(former: Former, address: str) -> int:
    """The epoch of the owner key event naming the person, or 0."""
    key = _key(former.owner_key_event, address) if former.owner_key_event else None
    return key.epoch if key is not None and key.owner == former.pubkey else 0


def standing(former: Optional[Former], person: str, epoch: Optional[int], *,
             address: str, proofs: Optional[List[Tuple[int, Optional[str], int]]] = None,
             owner_epoch: Optional[int] = None) -> Standing:
    """Whether ``person`` counts as a member of the Redaktion for an
    action naming ``epoch`` (None: epoch 1), and as one who may decide.
    ``proofs`` and ``owner_epoch`` are what :class:`Formers` checked before."""
    if former is None or former.pubkey != person:
        return Standing()
    e = 1 if epoch is None else epoch
    if proofs is None:
        proofs = [p for p in (_proof(g, person, address) for g in former.grants) if p]
    owner = _owner_epoch(former, address) if owner_epoch is None else owner_epoch
    owner_reaches = owner >= e > 0
    reaching = [at for at, _role, _t in proofs if at >= e] + ([owner] if owner_reaches else [])
    if not reaching:
        return Standing()
    g = min(reaching)
    roles = sorted(((at, t, role) for at, role, t in proofs if role is not None and at <= g),
                   reverse=True)
    return Standing(member=True,
                    decides=owner_reaches or bool(roles and roles[0][2] == "redaktion"))


class Formers:
    """The Formers of one submission at ``address``, joined by pubkey;
    each grant is checked once. ``epochs`` holds the epoch of each
    revision verified here (its signed body's), by revision."""

    def __init__(self, address: str) -> None:
        self.address = address
        self.epochs: Dict[int, int] = {}
        self._formers: Dict[str, Former] = {}
        self._checked: Dict[str, Tuple[list, int]] = {}

    def saw(self, revision: int, epoch: int) -> None:
        """``revision`` verified, and its signed body names ``epoch``."""
        if formats.is_int(epoch, low=1):
            self.epochs[revision] = epoch

    def join(self, formers: Iterable[Former]) -> None:
        for former in formers:
            known = self._formers.get(former.pubkey)
            if known is not None:
                grants = known.grants + tuple(g for g in former.grants
                                              if g not in known.grants)
                former = Former(former.pubkey, grants[:MAX_GRANTS],
                                former.owner_key_event or known.owner_key_event)
            if former != known:
                self._formers[former.pubkey] = former
                self._checked.pop(former.pubkey, None)

    def _standing(self, person: str, epoch: int) -> Standing:
        former = self._formers.get(person)
        if former is None:
            return Standing()
        if person not in self._checked:
            self._checked[person] = (
                [p for p in (_proof(g, person, self.address) for g in former.grants) if p],
                _owner_epoch(former, self.address))
        proofs, owner = self._checked[person]
        return standing(former, person, epoch, address=self.address, proofs=proofs,
                        owner_epoch=owner)

    def decides(self, person: str, name: str, body: Mapping[str, Any], *,
                decisions: Iterable[str]) -> bool:
        """Whether ``person``'s verified action ``name`` with ``body``
        verifies through the path of people who left: a decision names
        the epoch of the revision ``expected_revision`` names, a correction
        its own, and both need one who may decide; :data:`MEMBER_ACTIONS`
        name epoch 1 and need a member (contract 10). A revision not
        opened here lies between the epochs of those around it (they never
        fall): the action verifies only if it would at every epoch there,
        so nothing has to be read for it."""
        if name == "correction":
            epoch = body.get("epoch")
            return formats.is_int(epoch, low=1) and self._standing(person, epoch).decides
        if name in MEMBER_ACTIONS:
            return self._standing(person, 1).member
        if name not in decisions:
            return False
        revision = body.get("expected_revision")
        if not formats.is_int(revision, low=1):
            return False
        low, high = epoch_span(revision, self.epochs)
        if high is None or high - low > _MOST_EPOCHS:
            return False
        return all(self._standing(person, e).decides for e in range(low, high + 1))


def epoch_span(revision: int, epochs: Mapping[int, int]) -> Tuple[int, Optional[int]]:
    """The epochs ``revision`` may have: its own when it was opened, else
    from the nearest opened revision's below (or 1) to the nearest's
    above (None when none above was opened)."""
    if revision in epochs:
        return epochs[revision], epochs[revision]
    below = [e for r, e in epochs.items() if r < revision]
    above = [e for r, e in epochs.items() if r > revision]
    return (max(below) if below else 1), (min(above) if above else None)


__all__ = ["Former", "Formers", "Grant", "MAX_FORMERS", "MEMBER_ACTIONS", "Standing",
           "epoch_span",
           "parse_formers", "standing"]
