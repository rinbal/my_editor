# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the writer's two private STANDUP lists hold (editorial contract
13), apart from how they are read and written (``nostr/standup/lists.py``,
the Qt lists on the account's relays).

- The submissions list: records (contract 13 at 0456312), each a
  :class:`SubmissionEntry`, a :class:`Tombstone` for one removed, or a
  :class:`ForeignItem` for what this app cannot read. A record's
  identity is its ``sid`` and the hash of its status key
  (:func:`record_key`): two devices building one round each have
  theirs. The merge keeps the greater record of each identity under one
  order (:func:`_rank`: stored, newer, a tombstone at equal time, the
  greater canonical hash), so every order gives the same list; what a
  ``sid`` shows is :func:`shown_for`. Whether a write stored an entry is
  :func:`holds_exactly`.
- The read state: the last ``seq`` read per key, merged by the highest
  (:func:`merge_seen`).

Plain values and functions, no Qt: a list entry is checked, read,
merged and compared here, once, for every caller.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from dataclasses import dataclass, field, fields, replace
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from ..list_results import PublishResult
from . import formats
from .crypto import StandupCryptoError, secret_from_hex
from .formats import FormatError
from .js_values import MAX_SAFE, js_string, js_truthy, record_text

_MAX_TEXT = 1_000
# One wrap each for the Redaktion, the writer and every participant (15);
# a contributor's name is their 64-hex key.
_MAX_WRAPS = formats.MAX_PARTICIPANTS + 2
_MAX_WRAP_NAME = 64


# --------------------------------------------------------------------------- #
# The submissions list                                                         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SubmissionEntry:
    """One submission in the writer's list (13). ``s_sub`` and
    ``status_key`` are hex. ``envelope_sha256`` and ``wraps_sha256`` are
    what the app sent, as the receipt names them (12.4): a lookup after
    a restart confirms the submission only by all four of revision,
    revision hash and both (both None for a work by link, which has no
    envelope). A contributor's entry has no status key, and none of
    ``s_sub``, ``revision``, ``revision_hash`` or the two hashes until
    they opened their wrap. ``extra`` holds the fields this app does not
    know (a newer app's), written back as they came."""

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
    envelope_sha256: Optional[str]
    # name -> hex; a mapping, so left out of the hash (as ``extra``).
    wraps_sha256: Optional[Mapping[str, str]] = field(hash=False)
    updated_at: int
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, hash=False)
    # The app holds the server's receipt for this sid with this status key
    # (its own send, or a lookup): true from then on (13, 72d5ddc). False
    # before, for a contributor, and for an entry of a list written before
    # the field (read as not known to be stored).
    stored: bool = False
    # The record as it was read, with :meth:`to_json` of the entry then:
    # while the entry is unchanged, the list hashes and writes it back as
    # read, no field added or dropped (:func:`record_json`). None for one
    # built here.
    read_as: Optional[Tuple[Any, Any]] = field(default=None, compare=False, hash=False,
                                               repr=False)

    def to_json(self) -> Dict[str, Any]:
        value = dict(self.extra, **{name: getattr(self, name) for name in _ENTRY_FIELDS})
        if self.wraps_sha256 is not None:
            value["wraps_sha256"] = dict(self.wraps_sha256)
        return value


_ENTRY_FIELDS = tuple(f.name for f in fields(SubmissionEntry) if f.name not in ("extra",
                                                                               "read_as"))
# Fields a list written before them may lack: read as their default.
_LATER_FIELDS = frozenset({"stored"})


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
            _need_sent_hashes(entry)
        else:
            formats.need(entry.status_key is None, "a contributor has no status key")
            if None in opened:
                formats.need(opened == (None, None, None) and entry.envelope_sha256 is None
                             and entry.wraps_sha256 is None,
                             "a contributor keeps the key, the revision and what was sent "
                             "once the wrap opened")
            else:
                _need_sent_hashes(entry)
        formats.need(isinstance(entry.title, str) and len(entry.title) <= formats.MAX_TITLE_CHARS,
                     "the title")
        formats.need(entry.kind in formats.KINDS, "the kind")
        formats.need(isinstance(entry.d, str) and len(entry.d) <= _MAX_TEXT, "the d tag")
        formats.need(formats.is_int(entry.round, low=1), "the round")
        if entry.revision is not None:
            formats.need(formats.is_int(entry.revision, low=1), "the revision")
            formats.need(formats.is_hex64(entry.revision_hash), "the revision hash")
        formats.need(formats.is_int(entry.updated_at), "updated_at")
        formats.need(isinstance(entry.stored, bool) and not (entry.stored
                                                             and entry.role != "writer"),
                     "whether the server stored it")
        formats.need(isinstance(entry.extra, Mapping)
                     and not set(entry.extra) & set(_ENTRY_FIELDS), "the other fields")
    except (FormatError, StandupCryptoError) as exc:
        return str(exc)
    return None


def _need_sent_hashes(entry: SubmissionEntry) -> None:
    """What was sent, as the receipt names it (12.4, 13): the envelope's
    hash with the hash of each wrap by name, or both None for a work by
    link, which has no envelope (as the web app reads them)."""
    if entry.envelope_sha256 is None:
        formats.need(entry.wraps_sha256 is None, "a work by link sends no envelope")
        return
    formats.need(formats.is_hex64(entry.envelope_sha256), "the envelope's hash")
    wraps = entry.wraps_sha256
    formats.need(isinstance(wraps, Mapping) and 0 < len(wraps) <= _MAX_WRAPS
                 and all(isinstance(name, str) and 0 < len(name) <= _MAX_WRAP_NAME
                         and formats.is_hex64(value) for name, value in wraps.items()),
                 "the hashes of the wraps")


def entry_from_json(value: Any) -> Optional[SubmissionEntry]:
    """An entry read from the list, or None when this app cannot read it
    (a broken one, or a newer app's: another ``v``, role or kind). Fields
    it does not know are kept in ``extra``."""
    if not isinstance(value, dict) or value.get("deleted") is not None:
        return None
    if "v" in value and not (formats.is_int(value["v"]) and value["v"] == 1):
        return None
    if not set(_ENTRY_FIELDS) - _LATER_FIELDS <= set(value):
        return None
    entry = SubmissionEntry(
        **{name: value[name] for name in _ENTRY_FIELDS if name in value},
        extra={key: item for key, item in value.items() if key not in _ENTRY_FIELDS})
    if entry_problem(entry):
        return None
    return replace(entry, read_as=(copy.deepcopy(value), entry.to_json()))


def status_key_hash(status_key: str) -> str:
    """What a keyed tombstone names an entry by: the hash of its status
    key (hex of the key's bytes, 13 at 72d5ddc)."""
    return hashlib.sha256(bytes.fromhex(status_key)).hexdigest()


@dataclass(frozen=True)
class Tombstone:
    """A removed record (13 at 0456312): it stays in the list, a record of
    the identity its ``status_key_hash`` names (one without, from an
    older list, of the identity with no key), and the merge never lets it
    remove a stored entry. This app writes only keyed ones
    (:func:`tombstone_for`)."""

    sid: str
    updated_at: int
    status_key_hash: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {"sid": self.sid, "deleted": True}
        if self.status_key_hash is not None:
            value["status_key_hash"] = self.status_key_hash
        value["updated_at"] = self.updated_at
        return value


def tombstone_for(entry: "SubmissionEntry", updated_at: int) -> Tombstone:
    """The tombstone that removes ``entry`` and nothing else: keyed by its
    status key (a writer's entry has one)."""
    return Tombstone(sid=entry.sid, updated_at=updated_at,
                     status_key_hash=(status_key_hash(entry.status_key)
                                      if entry.status_key is not None else None))


@dataclass(frozen=True)
class ForeignItem:
    """An item this app cannot read: a newer app's, or a broken one. Kept
    as it came and written back unchanged, so this app never deletes
    what another wrote. Merged by record like any item when it has a
    64-hex ``sid`` and an integer ``updated_at``; else (``sid`` None)
    kept once per canonical text (:func:`_raw_key`)."""

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
    # Any integer the other apps read exactly (13: "an integer updated_at"),
    # negative too.
    keyed = formats.is_hex64(sid) and formats.is_int(updated_at, low=-MAX_SAFE)
    if keyed and value.get("deleted") is True:
        if set(value) == {"sid", "deleted", "updated_at"}:
            return Tombstone(sid=sid, updated_at=updated_at)
        if set(value) == {"sid", "deleted", "status_key_hash", "updated_at"} \
                and formats.is_hex64(value["status_key_hash"]):
            return Tombstone(sid=sid, updated_at=updated_at,
                             status_key_hash=value["status_key_hash"])
    return ForeignItem(sid=sid if keyed else None, updated_at=updated_at if keyed else 0,
                       raw=value)


@dataclass(frozen=True)
class SubmissionItems:
    """What the list holds: its records (13 at 0456312), one per identity
    (:func:`record_key`), so a ``sid`` may have several (two devices
    built its round, each with its own keys); the entries this app reads,
    the tombstones, and the items it cannot read."""

    items: Tuple[ListItem, ...] = ()

    @property
    def entries(self) -> Tuple[SubmissionEntry, ...]:
        """The entry each ``sid`` shows (:func:`shown_for`), once each."""
        shown = (shown_for(self, sid) for sid in _sids(self))
        return tuple(item for item in shown if isinstance(item, SubmissionEntry))

    @property
    def unread(self) -> Tuple[ForeignItem, ...]:
        return tuple(item for item in self.items if isinstance(item, ForeignItem))


def _sids(items: SubmissionItems) -> List[str]:
    """Every ``sid`` the records name, once each, in list order."""
    return list(dict.fromkeys(item.sid for item in items.items if item.sid is not None))


def record_key(item: ListItem) -> str:
    """A record's identity within its ``sid`` (13 at 0456312): an entry
    with a 64-hex status key, the hash of the key's bytes; a tombstone,
    the hash it names; anything else, "" (a contributor's entry, an
    unkeyed tombstone, an item that names no key)."""
    if isinstance(item, Tombstone):
        return item.status_key_hash or ""
    if is_tombstone(item):
        # As every app names it (the reference, STANDUP): its value as
        # JavaScript's String() writes it, "" when it is falsy.
        named = item.raw.get("status_key_hash")
        return js_string(named) if js_truthy(named) else ""
    key = _status_key_of(item)
    return status_key_hash(key) if key is not None else ""


def _identity(item: ListItem) -> Tuple[str, str]:
    if item.sid is None:
        return ("", _raw_key(item.to_json()))
    return (item.sid, record_key(item))


def _rank(item: ListItem) -> Tuple[bool, int, bool, str]:
    """The one order of the records of one identity (13 at 0456312): an
    entry the server stored beats every other record, so nothing removes
    it; then the newer ``updated_at``; at equal time a tombstone; then
    the greater SHA-256 of the record's canonical JSON, the record as it
    was read (:func:`record_json`, :func:`record_text`). A maximum under
    one order: every merge of the same records, in any order, on any
    device or app, keeps the same one."""
    return (_is_stored(item), item.updated_at, is_tombstone(item),
            hashlib.sha256(record_text(record_json(item)).encode("utf-8")).hexdigest())


def record_json(item: ListItem) -> Any:
    """A record as the list holds it: as it was read, with no field
    added, defaulted or dropped (a missing ``stored`` stays missing, an
    unknown field stays), unless it changed here since; one built or
    changed here as this app writes it. What the merge hashes and what a
    write puts back."""
    if isinstance(item, SubmissionEntry) and item.read_as is not None:
        raw, then = item.read_as
        if item.to_json() == then:
            return raw
    return item.to_json()


def merge_items(remote: SubmissionItems, local: SubmissionItems) -> SubmissionItems:
    """The union of both by record (13 at 0456312): of each identity the
    greater record (:func:`_rank`) is kept, on a tie the remote one. The
    order of ``remote`` is kept and new records go at the end, so the
    pages before the change read as they did and are not signed again:
    each page signed costs a request through a signer app. The one merge
    of the list: every read, write and the vectors go through it."""
    merged: Dict[Tuple[str, str], ListItem] = {}
    for item in remote.items + local.items:
        identity = _identity(item)
        known = merged.get(identity)
        if known is None or _rank(item) > _rank(known):
            merged[identity] = item
    return SubmissionItems(items=tuple(merged.values()))


def kept_of(known: Optional[ListItem], new: ListItem) -> ListItem:
    """Of two records of one identity, the one the merge keeps."""
    if known is None:
        return new
    return new if _rank(new) > _rank(known) else known


def shown_for(items: Optional[SubmissionItems], sid: str) -> Optional[ListItem]:
    """What ``sid`` shows (13 at 0456312), of its records that are not
    tombstones: the entry the server stored (with several, the smallest
    key); without one, the newest (at equal time the smallest key); None
    when it has none."""
    live = [item for item in (items.items if items is not None else ())
            if item.sid == sid and not is_tombstone(item)]
    stored = sorted((item for item in live if _is_stored(item)), key=record_key)
    if stored:
        return stored[0]
    live.sort(key=lambda item: (-item.updated_at, record_key(item)))
    return live[0] if live else None


def record_of(items: Optional[SubmissionItems], sid: str, key: str) -> Optional[ListItem]:
    """The record of identity (``sid``, ``key``) ``items`` hold, or None."""
    return next((item for item in (items.items if items is not None else ())
                 if item.sid == sid and record_key(item) == key), None)


def records_of(items: Optional[SubmissionItems], sid: str) -> Tuple[ListItem, ...]:
    """Every record of ``sid``, tombstones too."""
    return tuple(item for item in (items.items if items is not None else ()) if item.sid == sid)


def is_tombstone(item: ListItem) -> bool:
    """Whether ``item`` is a removal: a tombstone, or one this app cannot
    read that says it is deleted (as the other apps read ``deleted``:
    any value JavaScript takes for true)."""
    if isinstance(item, Tombstone):
        return True
    raw = item.raw if isinstance(item, ForeignItem) else None
    return isinstance(raw, dict) and js_truthy(raw.get("deleted"))


def _is_stored(item: ListItem) -> bool:
    """Whether the item records a submission the server stored: an entry
    marked so, or an item this app cannot read that says so itself."""
    if isinstance(item, SubmissionEntry):
        return item.stored
    raw = item.raw if isinstance(item, ForeignItem) else None
    return isinstance(raw, dict) and raw.get("stored") is True and not is_tombstone(item)


def _status_key_of(item: ListItem) -> Optional[str]:
    if isinstance(item, SubmissionEntry):
        return item.status_key if formats.is_hex64(item.status_key) else None
    raw = item.raw if isinstance(item, ForeignItem) else None
    key = raw.get("status_key") if isinstance(raw, dict) else None
    return key if formats.is_hex64(key) else None


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
    # A read across two generations may hold a record twice: the merge
    # keeps the greater.
    return merge_items(SubmissionItems(tuple(item_from_json(item) for item in items)),
                       SubmissionItems())


def encode_items(value: SubmissionItems, now: int) -> dict:
    """The items as written: each record as it was read while unchanged
    (:func:`record_json`), so a write never flips a record's bytes."""
    return {"items": [record_json(item) for item in value.items]}


class RecordStatus(Enum):
    """What recording an entry came to."""

    STORED = "stored"            # the list on the relays holds exactly this entry
    SUPERSEDED = "superseded"    # it holds another record in its place: do not send
    NOT_STORED = "not stored"    # the write did not end on the relays (``publish`` says why)


@dataclass(frozen=True)
class RecordResult:
    """One entry recorded: ``entry`` as recorded (its ``updated_at``
    raised), the write's ``publish`` result, and, when SUPERSEDED, what
    the list holds instead (``on_list``: the record of its keys, another
    device's revision or a tombstone, or else what its ``sid`` shows)."""

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
    """Whether ``items`` hold exactly ``entry`` as its record (the fields
    a lookup and an opening need: revision, revision hash, the
    envelope's and the wraps' hashes, ``s_sub``, ``status_key``); None
    when they hold no record of its identity."""
    item = record_of(items, entry.sid, record_key(entry))
    if item is None:
        return None
    return (isinstance(item, SubmissionEntry) and item.role == entry.role
            and item.revision == entry.revision and item.revision_hash == entry.revision_hash
            and same_sent(item, entry)
            and _same_secret(item.s_sub, entry.s_sub)
            and _same_secret(item.status_key, entry.status_key))


def same_sent(entry: SubmissionEntry, other: Any) -> bool:
    """Whether ``other`` (an entry, or a ``views.Receipt``: both have
    ``envelope_sha256`` and ``wraps_sha256``) names the same envelope and
    the same wraps, by name, as ``entry`` sent (12.4). An entry of a work
    by link names neither (it has no envelope): only that stays to
    compare."""
    if entry.wraps_sha256 is None:
        return entry.envelope_sha256 is None and other.envelope_sha256 is None
    return (other.envelope_sha256 == entry.envelope_sha256
            and isinstance(other.wraps_sha256, Mapping)
            and dict(other.wraps_sha256) == dict(entry.wraps_sha256))


def same_keys(a: SubmissionEntry, b: SubmissionEntry) -> bool:
    """Whether two entries of one ``sid`` hold the same keys (role,
    ``s_sub``, ``status_key``): the same run's submission, maybe at
    another revision. Entries with other keys are another run's, and one
    must never silently replace the other (RG-M2b 1)."""
    return (a.role == b.role and _same_secret(a.s_sub, b.s_sub)
            and _same_secret(a.status_key, b.status_key))


def stored_elsewhere(item: Optional[ListItem], entry: SubmissionEntry) -> bool:
    """Whether ``item``, what ``entry``'s sid shows, is the entry another
    device stored that round with (``stored``, other keys): what a
    revision 1 refused as ``stale_revision`` waits for (contract 2 at
    72d5ddc)."""
    return isinstance(item, SubmissionEntry) and item.sid == entry.sid and item.stored \
        and not same_keys(item, entry)


def _same_secret(a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a is b
    return hmac.compare_digest(a, b)


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
    "entry_from_json", "entry_problem", "higher_seen", "holds_exactly", "is_tombstone",
    "item_from_json", "kept_of", "merge_items", "merge_seen", "record_key", "record_of",
    "records_of", "same_keys", "same_sent", "shown_for", "status_key_hash", "stored_elsewhere",
    "tombstone_for",
]
