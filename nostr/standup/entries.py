# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the writer's two private STANDUP lists hold (editorial contract
13), apart from how they are read and written (``nostr/standup/lists.py``,
the Qt lists on the account's relays).

- The submissions list: one :class:`SubmissionEntry` per submission, a
  :class:`Tombstone` for one removed, a :class:`ForeignItem` for what
  this app cannot read; merged by ``sid``, the newest ``updated_at``
  wins (:func:`merge_items`). Whether a write stored an entry is
  :func:`holds_exactly`; whether it would replace other secrets,
  :func:`takes_place_of`.
- The read state: the last ``seq`` read per key, merged by the highest
  (:func:`merge_seen`).

Plain values and functions, no Qt: a list entry is checked, read,
merged and compared here, once, for every caller.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from ..list_results import PublishResult
from . import formats
from .crypto import StandupCryptoError, secret_from_hex
from .formats import FormatError

_MAX_TEXT = 1_000


# --------------------------------------------------------------------------- #
# The submissions list                                                         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SubmissionEntry:
    """One submission in the writer's list (13). ``s_sub`` and
    ``status_key`` are hex. A contributor's entry has no status key, and
    no ``s_sub``, ``revision`` or ``revision_hash`` until they opened
    their wrap. ``extra`` holds the fields this app does not know (a
    newer app's), written back as they came."""

    sid: str
    publication: str
    server: str
    role: str
    s_sub: Optional[str] = field(repr=False)
    status_key: Optional[str] = field(repr=False)
    title: str
    kind: str
    d: str
    round: int
    revision: Optional[int]
    revision_hash: Optional[str]
    updated_at: int
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, hash=False)

    def to_json(self) -> Dict[str, Any]:
        return dict(self.extra, **{name: getattr(self, name) for name in _ENTRY_FIELDS})


_ENTRY_FIELDS = tuple(f.name for f in fields(SubmissionEntry) if f.name != "extra")


def entry_problem(entry: SubmissionEntry) -> Optional[str]:
    """Why ``entry`` is not one the list may hold, or None."""
    try:
        formats.need(formats.is_hex64(entry.sid), "the submission id")
        formats.parse_address(entry.publication)
        formats.need(isinstance(entry.server, str) and 0 < len(entry.server) <= _MAX_TEXT,
                     "the server")
        formats.need(entry.role in formats.ROLES, "the role")
        if entry.s_sub is not None:
            formats.need(formats.is_hex64(entry.s_sub), "the submission key")
            secret_from_hex(entry.s_sub)
        formats.need(entry.status_key is None or formats.is_hex64(entry.status_key),
                     "the status key")
        opened = (entry.s_sub, entry.revision, entry.revision_hash)
        if entry.role == "writer":
            formats.need(entry.status_key is not None and None not in opened,
                         "a writer keeps the submission key, the status key and the revision")
        else:
            formats.need(entry.status_key is None, "a contributor has no status key")
            formats.need(None not in opened or opened == (None, None, None),
                         "a contributor keeps the key and the revision once the wrap opened")
        formats.need(isinstance(entry.title, str) and len(entry.title) <= formats.MAX_TITLE_CHARS,
                     "the title")
        formats.need(entry.kind in formats.KINDS, "the kind")
        formats.need(isinstance(entry.d, str) and len(entry.d) <= _MAX_TEXT, "the d tag")
        formats.need(formats.is_int(entry.round, low=1), "the round")
        if entry.revision is not None:
            formats.need(formats.is_int(entry.revision, low=1), "the revision")
            formats.need(formats.is_hex64(entry.revision_hash), "the revision hash")
        formats.need(formats.is_int(entry.updated_at), "updated_at")
        formats.need(isinstance(entry.extra, Mapping)
                     and not set(entry.extra) & set(_ENTRY_FIELDS), "the other fields")
    except (FormatError, StandupCryptoError) as exc:
        return str(exc)
    return None


def entry_from_json(value: Any) -> Optional[SubmissionEntry]:
    """An entry read from the list, or None when this app cannot read it
    (a broken one, or a newer app's: another ``v``, role or kind). Fields
    it does not know are kept in ``extra``."""
    if not isinstance(value, dict) or value.get("deleted") is not None:
        return None
    if "v" in value and not (formats.is_int(value["v"]) and value["v"] == 1):
        return None
    if not set(_ENTRY_FIELDS) <= set(value):
        return None
    entry = SubmissionEntry(
        **{name: value[name] for name in _ENTRY_FIELDS},
        extra={key: item for key, item in value.items() if key not in _ENTRY_FIELDS})
    return None if entry_problem(entry) else entry


@dataclass(frozen=True)
class Tombstone:
    """A removed submission (13): it stays in the list, so the removal
    wins over every older copy of the entry, on every device."""

    sid: str
    updated_at: int

    def to_json(self) -> Dict[str, Any]:
        return {"sid": self.sid, "deleted": True, "updated_at": self.updated_at}


@dataclass(frozen=True)
class ForeignItem:
    """An item this app cannot read: a newer app's, or a broken one. Kept
    as it came and written back unchanged, so this app never deletes
    what another wrote. Merged by ``sid`` like any item when it has a
    64-hex one and an integer ``updated_at``; else (``sid`` None) kept
    once per canonical text (:func:`_raw_key`)."""

    sid: Optional[str]
    updated_at: int
    raw: Any = field(repr=False)

    def to_json(self) -> Any:
        return self.raw


ListItem = Union[SubmissionEntry, Tombstone, ForeignItem]


def item_from_json(value: Any) -> ListItem:
    """One item of the list, read once into its type."""
    entry = entry_from_json(value)
    if entry is not None:
        return entry
    sid = value.get("sid") if isinstance(value, dict) else None
    updated_at = value.get("updated_at") if isinstance(value, dict) else None
    keyed = formats.is_hex64(sid) and formats.is_int(updated_at)
    if keyed and value.get("deleted") is True and set(value) == {"sid", "deleted", "updated_at"}:
        return Tombstone(sid=sid, updated_at=updated_at)
    return ForeignItem(sid=sid if keyed else None, updated_at=updated_at if keyed else 0,
                       raw=value)


@dataclass(frozen=True)
class SubmissionItems:
    """What the list holds, one item per ``sid``: the entries this app
    reads, the tombstones, and the items it cannot read."""

    items: Tuple[ListItem, ...] = ()

    @property
    def entries(self) -> Tuple[SubmissionEntry, ...]:
        return tuple(item for item in self.items if isinstance(item, SubmissionEntry))

    @property
    def unread(self) -> Tuple[ForeignItem, ...]:
        return tuple(item for item in self.items if isinstance(item, ForeignItem))


def merge_items(remote: SubmissionItems, local: SubmissionItems) -> SubmissionItems:
    """The union of both (13): by ``sid`` the newest ``updated_at`` wins,
    on a tie the remote one; an item without a ``sid`` is kept once. The
    order of ``remote`` is kept and new items go at the end, so the pages
    before the change read as they did and are not signed again: each
    page signed costs a request through a signer app."""
    merged: Dict[str, ListItem] = {}
    for item in remote.items + local.items:
        key = item.sid if item.sid is not None else _raw_key(item.to_json())
        known = merged.get(key)
        if known is None or item.updated_at > known.updated_at:
            merged[key] = item
    return SubmissionItems(items=tuple(merged.values()))


def _raw_key(value: Any) -> str:
    """The key an item without ``sid`` or ``updated_at`` is kept once by:
    its canonical text (13). JSON that canonical JSON cannot hold (a
    float, a non-ASCII key) is still JSON, so it gets the same form with
    sorted keys; the two forms never meet, the prefix tells them apart."""
    try:
        return "raw:" + formats.canonical_json(value)
    except FormatError:
        return "json:" + json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                    sort_keys=True)


def decode_items(payload: dict) -> SubmissionItems:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("the submissions list has no items")
    # A read across two generations may hold an entry twice: the merge
    # keeps the newest.
    return merge_items(SubmissionItems(tuple(item_from_json(item) for item in items)),
                       SubmissionItems())


def encode_items(value: SubmissionItems, now: int) -> dict:
    return {"items": [item.to_json() for item in value.items]}


class RecordStatus(Enum):
    """What recording an entry came to."""

    STORED = "stored"            # the list on the relays holds exactly this entry
    SUPERSEDED = "superseded"    # it holds another copy of this sid, newer: do not send
    NOT_STORED = "not stored"    # the write did not end on the relays (``publish`` says why)


@dataclass(frozen=True)
class RecordResult:
    """One entry recorded: ``entry`` as recorded (its ``updated_at``
    raised), the write's ``publish`` result, and, when SUPERSEDED, what
    the list holds for its ``sid`` instead (``on_list``: another
    device's entry, a tombstone or an item this app cannot read)."""

    status: RecordStatus
    entry: SubmissionEntry
    publish: PublishResult
    on_list: Optional[Any] = field(default=None, repr=False)

    @property
    def stored(self) -> bool:
        """The submission may be sent: the list holds exactly this entry."""
        return self.status is RecordStatus.STORED

    @property
    def reason(self) -> str:
        return self.publish.reason


def holds_exactly(items: Optional[SubmissionItems], entry: SubmissionEntry) -> Optional[bool]:
    """Whether ``items`` hold exactly ``entry`` for its ``sid`` (the
    fields a lookup and an opening need: revision, revision hash,
    ``s_sub``, ``status_key``); None when they hold nothing for it."""
    item = item_for(items, entry.sid)
    if item is None:
        return None
    return (isinstance(item, SubmissionEntry) and item.role == entry.role
            and item.revision == entry.revision and item.revision_hash == entry.revision_hash
            and _same_secret(item.s_sub, entry.s_sub)
            and _same_secret(item.status_key, entry.status_key))


def same_keys(a: SubmissionEntry, b: SubmissionEntry) -> bool:
    """Whether two entries of one ``sid`` hold the same keys (role,
    ``s_sub``, ``status_key``): the same run's submission, maybe at
    another revision. Entries with other keys are another run's, and one
    must never silently replace the other (RG-M2b 1)."""
    return (a.role == b.role and _same_secret(a.s_sub, b.s_sub)
            and _same_secret(a.status_key, b.status_key))


def takes_place_of(item: Optional[ListItem], entry: SubmissionEntry) -> bool:
    """Whether writing ``entry`` would replace ``item`` of its ``sid``
    with other secrets: an entry with other keys, or one this app cannot
    read (it may hold the keys of a stored revision). One ``sid`` has one
    set of secrets for ever (contract 2, 13 "Entries")."""
    if isinstance(item, ForeignItem):
        return True
    return isinstance(item, SubmissionEntry) and not same_keys(item, entry)


def _same_secret(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a is b
    return hmac.compare_digest(a, b)


def item_for(items: Optional[SubmissionItems], sid: str) -> Optional[ListItem]:
    """What ``items`` hold for ``sid``, or None."""
    return next((i for i in (items.items if items is not None else ()) if i.sid == sid), None)


# --------------------------------------------------------------------------- #
# The read state                                                               #
# --------------------------------------------------------------------------- #

def higher_seen(earlier: Any, later: Any) -> Any:
    """Of two values of one key, the higher whole number; where only one
    is a whole number, that one; else the earlier (13)."""
    if formats.is_int(earlier) and formats.is_int(later):
        return max(earlier, later)
    return later if formats.is_int(later) else earlier


def decode_seen(payload: dict) -> Dict[str, Any]:
    """Every key as it came: this app reads ``<sid>`` keys, and keeps the
    rest (STANDUP writes ``i:<sid>`` for a member's internal notes,
    contract 13, feat/redaktion 5c11475) so a write never drops them."""
    seen = payload.get("seen")
    if not isinstance(seen, dict):
        raise ValueError("the read state has no map")
    return dict(seen)


def merge_seen(remote: Dict[str, Any], local: Dict[str, Any]) -> Dict[str, Any]:
    """The read state's union (13): the highest whole number per key, for
    the keys this app reads and those it only keeps (where one side holds
    something else, the whole number; where neither does, the relays')."""
    merged = dict(remote)
    for key, value in local.items():
        merged[key] = higher_seen(merged[key], value) if key in merged else value
    return merged


__all__ = [
    "ForeignItem", "ListItem", "RecordResult", "RecordStatus", "SubmissionEntry",
    "SubmissionItems", "Tombstone", "decode_items", "decode_seen", "encode_items",
    "entry_from_json", "entry_problem", "higher_seen", "holds_exactly", "item_for",
    "item_from_json",
    "merge_items", "merge_seen", "same_keys", "takes_place_of",
]
