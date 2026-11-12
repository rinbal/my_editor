# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""People who left (editorial contract 10, "People who left"; M-4 item 2).

The member list holds today's members only. An action signed by someone
who has left or was removed since verifies from what the owner and the
publication key signed, never from the server's word: the read of a
submission serves a ``Former`` for each such signer, with the stored
grants that gave them the Redaktion's key and the versions of the key
events that named them owner (``owner_key_events``).

:func:`standing` is the one check, as ``formerStanding`` in the
contract's ``docs/editorial-fixtures/crosscheck.reference.mjs`` has it
(contract 569b3f8, vector ``former_member``). Membership goes by epoch:
a counting grant or owner key event at the action's epoch e or later
makes the person a member then. The role goes by time, an owner key
event's too: of the counting invitations and counting owner key events
(each one counting as ``redaktion``) signed at or before the action's
``created_at``, the latest decide, and all of them must have the role
``redaktion`` when several share that second. So a higher epoch never
grants more, and an earlier owner invited as a curator decides nothing
after that invitation. :class:`Formers` keeps the Formers of one
submission, joined by pubkey across reads, checks each grant once, and
picks the epoch an action names: a decision is checked at the epoch of
the current revision first, and only when it fails there at its own
revision's, which is then read with ``?revision=n``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from .. import events as nostr_events
from . import formats
from .formats import FormatError
from .readers import read_list, read_object, read_text

# At most 8 people in one answer (contract 10); a Former's grants prove
# the epochs its person's actions name, two at most for each, and its
# owner key events the latest and the one before each decision.
MAX_FORMERS = 8
MAX_GRANTS = 64
MAX_OWNER_KEY_EVENTS = 64
# Later than every signature: the standing as of the newest proof.
_LATEST = 2 ** 63
_MAX_GRANT_BODY = 1_048_576
ROLES = ("redaktion", "curator")
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
    owner_key_events: Tuple[Mapping[str, Any], ...] = ()


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
        owners = tuple(dict(read_object(e, "an owner key event")) for e in read_list(
            data.get("owner_key_events") or [], "a former member's owner key events",
            limit=MAX_OWNER_KEY_EVENTS))
        out.append(Former(pubkey=pubkey, grants=grants, owner_key_events=owners))
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


def proofs_of(former: Former, address: str) -> List[Tuple[int, Optional[str], int]]:
    """``(epoch, role or None, created_at)`` of each of ``former``'s
    grants and owner key events that counts for its person; an owner
    key event naming the person counts for its epoch, and as
    ``redaktion`` from when it was signed (contract 10)."""
    person = former.pubkey
    found = [p for p in (_proof(g, person, address) for g in former.grants) if p]
    for event in former.owner_key_events:
        key = _key(event, address)
        if key is not None and key.owner == person and formats.is_int(event.get("created_at")):
            found.append((key.epoch, "redaktion", event["created_at"]))
    return found


def standing(former: Optional[Former], person: str, epoch: Optional[int], at: int, *,
             address: str,
             proofs: Optional[List[Tuple[int, Optional[str], int]]] = None) -> Standing:
    """Whether ``person`` counts as a member of the Redaktion for an
    action naming ``epoch`` (None: epoch 1) signed at ``at`` (its
    ``created_at``), and as one who may decide. ``proofs`` are what
    :class:`Formers` checked before (:func:`proofs_of`)."""
    if former is None or former.pubkey != person:
        return Standing()
    e = 1 if epoch is None else epoch
    if proofs is None:
        proofs = proofs_of(former, address)
    if not any(of >= e for of, _role, _t in proofs):
        return Standing()
    before = [(t, role) for _of, role, t in proofs if role is not None and t <= at]
    latest = max((t for t, _role in before), default=None)
    redaktion = bool(before) and all(role == "redaktion" for t, role in before if t == latest)
    return Standing(member=True, decides=redaktion)


class Formers:
    """The Formers of one submission at ``address``, joined by pubkey;
    each grant is checked once. ``epochs`` holds the epoch of each
    revision verified here (its signed body's), by revision;
    ``want(revision)`` is asked once for a revision a decision must be
    checked at that is not read yet (``?revision=n``)."""

    def __init__(self, address: str,
                 want: Optional[Callable[[int], None]] = None) -> None:
        self.address = address
        self.epochs: Dict[int, int] = {}
        self._want = want
        self._wanted: Set[int] = set()
        self._formers: Dict[str, Former] = {}
        self._checked: Dict[str, list] = {}

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
                owners = known.owner_key_events + tuple(
                    e for e in former.owner_key_events if e not in known.owner_key_events)
                former = Former(former.pubkey, grants[:MAX_GRANTS],
                                owners[:MAX_OWNER_KEY_EVENTS])
            if former != known:
                self._formers[former.pubkey] = former
                self._checked.pop(former.pubkey, None)

    def _standing(self, person: str, epoch: int, at: int) -> Standing:
        former = self._formers.get(person)
        if former is None:
            return Standing()
        if person not in self._checked:
            self._checked[person] = proofs_of(former, self.address)
        return standing(former, person, epoch, at, address=self.address,
                        proofs=self._checked[person])

    def proven(self) -> FrozenSet[str]:
        """The people whose served grants or owner key events prove they
        were members (at epoch 1 or later): their messages are shown as
        the Redaktion's (RG-M4-a 7)."""
        return frozenset(p for p in self._formers if self._standing(p, 1, _LATEST).member)

    def decided_then(self, person: str, at: Any) -> bool:
        """Whether ``person`` could decide at ``at``, by the role rule
        (RG-M4-c 6); for a conversation note, ``at`` is when the server
        took it, never the rumor's own time, and as it names no epoch it
        counts as epoch 1 (contract 4 at 71e23dd). One who accepted
        as the Redaktion and was invited again as a curator later keeps the
        split card they proposed then: their structured notes, a split
        proposal among them, are read as a decider's."""
        return formats.is_int(at) and self._standing(person, 1, at).decides

    def decides(self, person: str, name: str, body: Mapping[str, Any], *,
                decisions: Iterable[str], at: Any) -> bool:
        """Whether ``person``'s verified action ``name`` with ``body``,
        signed at ``at``, verifies through the path of people who left
        (contract 10): a decision names the epoch of the revision
        ``expected_revision`` names, a correction its own, and both need
        one who may decide; :data:`MEMBER_ACTIONS` name epoch 1 and need a
        member. Epochs never fall and a higher one never grants more, so
        a decision that verifies at the current revision's epoch verifies
        at its own; only when it does not is its revision read (``want``)
        and the decision checked again once that revision's epoch is known."""
        if not formats.is_int(at):
            return False
        if name == "correction":
            epoch = body.get("epoch")
            return formats.is_int(epoch, low=1) and self._standing(person, epoch, at).decides
        if name in MEMBER_ACTIONS:
            return self._standing(person, 1, at).member
        if name not in decisions:
            return False
        revision = body.get("expected_revision")
        if not formats.is_int(revision, low=1):
            return False
        if revision in self.epochs:
            return self._standing(person, self.epochs[revision], at).decides
        current = max(self.epochs, default=0)
        if current > revision and self._standing(person, self.epochs[current], at).decides:
            return True
        if revision not in self._wanted:
            self._wanted.add(revision)
            if self._want is not None:
                self._want(revision)
        return False


__all__ = ["Former", "Formers", "Grant", "MAX_FORMERS", "MAX_OWNER_KEY_EVENTS",
           "MEMBER_ACTIONS", "Standing", "parse_formers", "proofs_of", "standing"]
