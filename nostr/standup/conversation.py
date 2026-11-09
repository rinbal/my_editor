# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The conversation about a submission (editorial contract 9).

Every message, reaction, taking back of a reaction, edit and structured
note is an unsigned rumor (NIP-59, 59.md), sealed (kind 13) and signed by
its sender, then gift-wrapped (kind 1059) to the room: the submission key
``P_sub`` for the conversation, the Redaktion's epoch key for internal
notes. Every participant holds the room's secret, so the seal's
encryption key is computed here, ``conversation_key(room_sk, sender)``,
which equals ``conversation_key(sender_sk, room)``. The only call to the
account's signer is ``sign_event`` for the seal; the gift wrap is signed
by a fresh key made here.

Reading checks everything 9.2 lists, and a little more: the wrap's id,
signature, kind and room; the seal's id, signature, kind and tags; the
rumor's id, that its author is the seal's signer (17.md: a server cannot
put words in someone's mouth), and that it belongs to this submission.
What fails a check is not shown (:class:`NotShown`).

:func:`fold` turns what was read into what a conversation shows: edits
by the author (newest wins), one reaction per person per message, a
taking back honoured only from the reaction's author (09.md, on the seal
pubkey), the server's ``seq`` for order.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .. import crypto as nostr_crypto
from .. import events as nostr_events
from . import formats, views
from .formats import FormatError

MESSAGE_KIND = 14
REACTION_KIND = 7
DELETION_KIND = 5
SEAL_KIND = 13
GIFT_WRAP_KIND = 1059

# The six reactions (9.1, plan D36).
REACTIONS = ("❤️", "\U0001f44d", "\U0001f44e", "\U0001f602", "‼️", "❓")

# Seal and wrap times are moved back by up to two days, each on its own
# (59.md; 17.md L59), so neither tells when a message was sent.
MAX_BACKDATE_SECONDS = 172_800

STRUCTURED_TYPES = ("split-proposal", "revision-note")
SHARE_ROLES = ("writer", "contributor", "publication")

# What the messages endpoint is told about a wrap it cannot read (12.4).
HINT_MESSAGE = "message"
HINT_REACTION = "reaction"
HINT_RETRACTION = "retraction"
HINT_EDIT = "edit"


class MessageRefused(FormatError):
    """A message that cannot be sent: too long, or holding a secret key
    (``formats.SecretKeyFound`` is the one with words to show)."""


class NotShown(ValueError):
    """A conversation item that failed a check. ``reason`` is for logs."""


# --------------------------------------------------------------------------- #
# Rumors (9.1)                                                                 #
# --------------------------------------------------------------------------- #

def build_rumor(kind: int, content: str, tags: List[List[str]], pubkey: str,
                created_at: int) -> dict:
    """An unsigned event with its NIP-01 id and no signature."""
    rumor = {"kind": kind, "content": content, "tags": [list(t) for t in tags],
             "pubkey": pubkey, "created_at": int(created_at)}
    rumor["id"] = nostr_events.compute_event_id(pubkey, rumor["created_at"], kind,
                                                rumor["tags"], content)
    return rumor


def rumor_size(rumor: dict) -> int:
    """Bytes of the serialized rumor (canonical JSON), what the seal carries."""
    return len(formats.canonical_bytes(rumor))


def check_rumor_size(rumor: dict) -> None:
    if rumor_size(rumor) > formats.MAX_RUMOR_BYTES:
        raise MessageRefused(f"the message is larger than {formats.MAX_RUMOR_BYTES} bytes")


def _room_tags(room: str, submission_id: str) -> List[List[str]]:
    if not formats.is_pubkey(room):
        raise MessageRefused("the room is not a valid key")
    if not formats.is_hex64(submission_id):
        raise MessageRefused("not a submission id")
    return [["p", room], ["submission", submission_id]]


def check_message_text(text: Any) -> str:
    """Message text that may be sent: text, not empty, at most 16,000
    characters, and no secret key in it (5, 9.1)."""
    if not isinstance(text, str) or not text.strip():
        raise MessageRefused("a message needs text")
    if len(text) > formats.MAX_MESSAGE_CHARS:
        raise MessageRefused(f"a message has at most {formats.MAX_MESSAGE_CHARS} characters")
    formats.refuse_secret_keys([text])
    return text


def message_rumor(*, room: str, submission_id: str, text: str, sender: str,
                  created_at: int, edit_of: Optional[str] = None) -> dict:
    """A message, or with ``edit_of`` the new text of one of the sender's
    own messages (9.3)."""
    tags = _room_tags(room, submission_id)
    if edit_of is not None:
        if not formats.is_hex64(edit_of):
            raise MessageRefused("an edit names the message it changes")
        tags.append(["edit", edit_of])
    rumor = build_rumor(MESSAGE_KIND, check_message_text(text), tags, sender, created_at)
    check_rumor_size(rumor)
    return rumor


def split_proposal_content(revision_hash: str, shares: Sequence[dict]) -> dict:
    """The content of a ``split-proposal`` (9.4): weights are whole
    percentages summing to 100."""
    if not formats.is_hex64(revision_hash):
        raise MessageRefused("a split names the revision it is for")
    cleaned = []
    for share in shares:
        if (not isinstance(share, dict) or not formats.is_pubkey(share.get("pubkey"))
                or not formats.is_int(share.get("weight"), high=100)
                or share.get("role") not in SHARE_ROLES):
            raise MessageRefused("a share needs a person, a whole percentage and a role")
        cleaned.append({"pubkey": share["pubkey"], "weight": share["weight"],
                        "role": share["role"]})
    if sum(s["weight"] for s in cleaned) != 100:
        raise MessageRefused("the shares must sum to 100")
    return {"v": 1, "revision_hash": revision_hash, "shares": cleaned}


def revision_note_content(revision: int, note: str) -> dict:
    if not formats.is_int(revision, low=1):
        raise MessageRefused("a note names its revision")
    return {"v": 1, "revision": revision, "note": check_message_text(note)}


def structured_rumor(*, room: str, submission_id: str, kind: str, content: dict, sender: str,
                     created_at: int) -> dict:
    """A structured note (9.4), its content canonical JSON."""
    if kind not in STRUCTURED_TYPES:
        raise MessageRefused("not a structured type of the contract")
    _check_structured(kind, content)
    tags = _room_tags(room, submission_id) + [["standup", kind]]
    rumor = build_rumor(MESSAGE_KIND, formats.canonical_json(content), tags, sender, created_at)
    formats.refuse_secret_keys(_structured_texts(content))
    check_rumor_size(rumor)
    return rumor


def reaction_rumor(*, room: str, submission_id: str, target_id: str, target_author: str,
                   emoji: str, sender: str, created_at: int) -> dict:
    if emoji not in REACTIONS:
        raise MessageRefused("not one of the six reactions")
    if not formats.is_hex64(target_id) or not formats.is_pubkey(target_author):
        raise MessageRefused("a reaction names a message and its author")
    tags = _room_tags(room, submission_id) + [["e", target_id], ["p", target_author],
                                              ["k", str(MESSAGE_KIND)]]
    return build_rumor(REACTION_KIND, emoji, tags, sender, created_at)


def retraction_rumor(*, room: str, submission_id: str, reaction_id: str, sender: str,
                     created_at: int) -> dict:
    """Taking back one's own reaction (kind 5 naming it)."""
    if not formats.is_hex64(reaction_id):
        raise MessageRefused("a taking back names the reaction")
    tags = _room_tags(room, submission_id) + [["e", reaction_id], ["k", str(REACTION_KIND)]]
    return build_rumor(DELETION_KIND, "", tags, sender, created_at)


def kind_hint(rumor: dict) -> str:
    """What the messages endpoint is told the wrap holds (12.4): a
    reaction, a taking back or an edit sends no notice, assigns nobody."""
    if rumor.get("kind") == REACTION_KIND:
        return HINT_REACTION
    if rumor.get("kind") == DELETION_KIND:
        return HINT_RETRACTION
    if formats.tag_value(rumor, "edit") is not None:
        return HINT_EDIT
    return HINT_MESSAGE


# --------------------------------------------------------------------------- #
# Seal and gift wrap (9.2)                                                     #
# --------------------------------------------------------------------------- #

def _backdated(now: float, rand: Callable[[int], int]) -> int:
    return int(now) - int(rand(MAX_BACKDATE_SECONDS + 1))


def seal_unsigned(rumor: dict, room_sk: bytes, *, now: float,
                  rand: Callable[[int], int] = secrets.randbelow) -> dict:
    """The seal for ``rumor``, unsigned: kind 13, no tags, the rumor
    encrypted with ``conversation_key(room_sk, sender)``."""
    check_rumor_size(rumor)
    sender = rumor["pubkey"]
    key = nostr_crypto.conversation_key(bytes(room_sk), bytes.fromhex(sender))
    content = nostr_crypto.encrypt(formats.canonical_json(rumor), key)
    return {"kind": SEAL_KIND, "pubkey": sender, "tags": [], "content": content,
            "created_at": _backdated(now, rand)}


def gift_wrap(seal: dict, room: str, *, now: float,
              rand: Callable[[int], int] = secrets.randbelow,
              eph_sk: Optional[bytes] = None) -> dict:
    """The gift wrap of a signed seal to ``room``, signed by a fresh key."""
    eph = eph_sk if eph_sk is not None else nostr_crypto.generate_secret_key()
    content = nostr_crypto.encrypt(formats.canonical_json(seal),
                                   nostr_crypto.conversation_key(eph, bytes.fromhex(room)))
    wrap = nostr_events.build_event(GIFT_WRAP_KIND, content, [["p", room]], sk=eph,
                                    created_at=_backdated(now, rand))
    if len(formats.canonical_bytes(wrap)) > formats.MAX_WRAP_EVENT_BYTES:
        raise MessageRefused("the wrapped message is larger than the server takes")
    return wrap


def seal_problem(signed: Any, unsigned: dict) -> Optional[str]:
    """Why the signer's answer is not the seal that was asked for."""
    if not isinstance(signed, dict):
        return "the signer returned something that is not an event"
    for name in ("kind", "pubkey", "tags", "content", "created_at"):
        if signed.get(name) != unsigned[name]:
            return f"the signer changed the seal's {name}"
    if not nostr_events.verify_event(signed):
        return "the signer returned a seal that does not verify"
    return None


def seal_and_wrap(signer, rumor: dict, room_sk: bytes, room: str, *,
                  on_success: Callable[[dict], None], on_failure: Callable[[str], None],
                  clock: Callable[[], float] = time.time,
                  rand: Callable[[int], int] = secrets.randbelow) -> None:
    """Seal ``rumor`` (signed through ``signer``, the one prompt) and wrap
    it to ``room``. ``on_success(wrap)``; ``on_failure(reason)``. Nothing
    is asked of the signer when the rumor is too large."""
    try:
        if nostr_crypto.get_public_key(bytes(room_sk)).hex() != room:
            raise MessageRefused("the room key does not match the room")
        unsigned = seal_unsigned(rumor, room_sk, now=clock(), rand=rand)
    except (FormatError, ValueError) as exc:
        on_failure(str(exc))
        return

    def signed(event: dict) -> None:
        problem = seal_problem(event, unsigned)
        if problem:
            on_failure(problem)
            return
        try:
            wrap = gift_wrap({k: event[k] for k in ("id", "pubkey", "created_at", "kind",
                                                    "tags", "content", "sig")},
                             room, now=clock(), rand=rand)
        except (FormatError, ValueError) as exc:
            on_failure(str(exc))
            return
        on_success(wrap)

    signer.sign_event(dict(unsigned, tags=[]), on_success=signed, on_failure=on_failure)


# --------------------------------------------------------------------------- #
# Reading (9.2)                                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Opened:
    """A conversation item that passed every check."""

    rumor: dict
    sender: str                 # the seal's signer, equal to the rumor's pubkey
    wrap_id: str
    seq: int = 0


def _decrypt_json(content: Any, key: bytes, what: str) -> dict:
    if not isinstance(content, str):
        raise NotShown(f"the {what} holds no ciphertext")
    try:
        value = json.loads(nostr_crypto.decrypt(content, key))
    except (ValueError, RecursionError):
        raise NotShown(f"the {what} does not open") from None
    if not isinstance(value, dict):
        raise NotShown(f"the {what} holds no event")
    return value


def _seal_tags_allowed(tags: Any) -> bool:
    """59.md: a seal's tags are empty. 17.md adds ``expiration`` for
    notices, so that is the one tag a reader accepts (9.2)."""
    return isinstance(tags, list) and all(
        isinstance(t, list) and len(t) == 2 and t[0] == "expiration"
        and isinstance(t[1], str) and t[1].isdigit() for t in tags)


def _is_rumor(value: dict) -> bool:
    return (isinstance(value.get("pubkey"), str) and formats.is_int(value.get("created_at"))
            and formats.is_int(value.get("kind")) and isinstance(value.get("content"), str)
            and isinstance(value.get("tags"), list)
            and all(isinstance(t, list) and all(isinstance(x, str) for x in t)
                    for t in value["tags"])
            and isinstance(value.get("id"), str))


def open_item(wrap: Any, room_sk: bytes, submission_id: str, *, seq: int = 0) -> Opened:
    """The rumor in a gift wrap, with every check of 9.2, or NotShown."""
    if not nostr_events.is_verified_event(wrap, kind=GIFT_WRAP_KIND):
        raise NotShown("not a gift wrap whose signature verifies")
    try:
        size = len(formats.canonical_bytes(wrap))
    except FormatError:
        raise NotShown("the gift wrap holds what JSON does not") from None
    if size > formats.MAX_WRAP_EVENT_BYTES:
        raise NotShown("larger than a conversation item may be")
    room = nostr_crypto.get_public_key(bytes(room_sk)).hex()
    if formats.tag_value(wrap, "p") != room:
        raise NotShown("the gift wrap is for another room")
    seal = _decrypt_json(wrap["content"], nostr_crypto.conversation_key(
        bytes(room_sk), bytes.fromhex(wrap["pubkey"])), "gift wrap")
    if not nostr_events.is_verified_event(seal, kind=SEAL_KIND):
        raise NotShown("the seal does not verify")
    if not _seal_tags_allowed(seal.get("tags")):
        raise NotShown("the seal carries tags")
    rumor = _decrypt_json(seal["content"], nostr_crypto.conversation_key(
        bytes(room_sk), bytes.fromhex(seal["pubkey"])), "seal")
    if not _is_rumor(rumor):
        raise NotShown("the seal holds no rumor")
    if nostr_events.compute_event_id(rumor["pubkey"], rumor["created_at"], rumor["kind"],
                                     rumor["tags"], rumor["content"]) != rumor["id"]:
        raise NotShown("the rumor's id does not match")
    if rumor["pubkey"] != seal["pubkey"]:
        raise NotShown("the rumor's author is not the seal's signer")
    if ["submission", submission_id] not in rumor["tags"]:
        raise NotShown("the rumor belongs to another submission")
    if formats.tag_value(rumor, "p") != room:
        raise NotShown("the rumor names another room")
    return Opened(rumor=rumor, sender=seal["pubkey"], wrap_id=wrap["id"], seq=int(seq))


@dataclass(frozen=True)
class OpenedTimeline:
    """The messages of a timeline that passed every check, in ``seq``
    order, and how many did not (9.2: they are not shown, and the person
    can be told that some could not be read)."""

    opened: Tuple[Opened, ...]
    not_shown: int


def open_items(items: Sequence[views.TimelineItem], room_sk: bytes,
               submission_id: str) -> OpenedTimeline:
    """Every message of ``items`` that passes the checks of 9.2; the
    timeline's events (no wrap) are not messages."""
    opened: List[Opened] = []
    not_shown = 0
    for item in items:
        if item.wrap is None:
            continue
        try:
            opened.append(open_item(item.wrap, room_sk, submission_id, seq=item.seq))
        except NotShown:
            not_shown += 1
    return OpenedTimeline(opened=tuple(sorted(opened, key=lambda o: o.seq)),
                          not_shown=not_shown)


# --------------------------------------------------------------------------- #
# What a conversation shows (9.3, 9.4)                                         #
# --------------------------------------------------------------------------- #

@dataclass
class Bubble:
    """A message as shown: its newest text, whether it was edited, and the
    reactions on it (one per person)."""

    id: str
    author: str
    created_at: int
    seq: int
    text: str
    edited: bool = False
    reactions: Dict[str, str] = field(default_factory=dict)        # person -> emoji
    structured: Optional[Tuple[str, dict]] = None                  # (type, content)


def _check_structured(kind: str, content: Any) -> dict:
    if not isinstance(content, dict) or content.get("v") != 1:
        raise MessageRefused("a structured note has version 1")
    if kind == "split-proposal":
        split_proposal_content(content.get("revision_hash"), content.get("shares") or [])
    elif kind == "revision-note":
        if not formats.is_int(content.get("revision"), low=1) or not isinstance(
                content.get("note"), str):
            raise MessageRefused("a revision note names its revision and has a note")
    return content


def _structured_texts(content: dict) -> List[str]:
    note = content.get("note")
    return [note] if isinstance(note, str) else []


def _structured_of(rumor: dict) -> Optional[Tuple[str, dict]]:
    kind = formats.tag_value(rumor, "standup")
    if kind is None:
        return None
    if kind not in STRUCTURED_TYPES:
        raise NotShown("an unknown structured type")
    try:
        content = json.loads(rumor["content"])
        _check_structured(kind, content)
    except (ValueError, RecursionError):
        raise NotShown("a structured note that does not read") from None
    return kind, content


def fold(opened: Sequence[Opened]) -> List[Bubble]:
    """The bubbles of a conversation, in ``seq`` order."""
    items = sorted(opened, key=lambda o: o.seq)
    bubbles: Dict[str, Bubble] = {}
    order: List[str] = []
    edits: Dict[str, List[Tuple[Tuple[int, int], str, str]]] = {}     # target -> versions
    reactions: Dict[str, Tuple[str, str, str, Tuple[int, int]]] = {}  # id -> target, who, emoji, when
    taken_back: Dict[str, set] = {}                                    # reaction id -> by whom

    for item in items:
        rumor = item.rumor
        when = (rumor["created_at"], item.seq)
        if rumor["kind"] == MESSAGE_KIND:
            target = formats.tag_value(rumor, "edit")
            if target is not None:
                edits.setdefault(target, []).append((when, item.sender, rumor["content"]))
                continue
            try:
                structured = _structured_of(rumor)
            except NotShown:
                continue
            if rumor["id"] in bubbles:
                continue
            bubbles[rumor["id"]] = Bubble(id=rumor["id"], author=item.sender,
                                          created_at=rumor["created_at"], seq=item.seq,
                                          text=rumor["content"], structured=structured)
            order.append(rumor["id"])
        elif rumor["kind"] == REACTION_KIND:
            target = formats.tag_value(rumor, "e")
            if target is not None and rumor["content"] in REACTIONS:
                reactions[rumor["id"]] = (target, item.sender, rumor["content"], when)
        elif rumor["kind"] == DELETION_KIND:
            target = formats.tag_value(rumor, "e")
            if target is not None:
                taken_back.setdefault(target, set()).add(item.sender)

    for target, versions in edits.items():
        bubble = bubbles.get(target)
        if bubble is None or bubble.structured is not None:
            continue
        own = [v for v in versions if v[1] == bubble.author]   # 9.3: anyone else's is ignored
        if own:
            bubble.text = max(own, key=lambda v: v[0])[2]
            bubble.edited = True

    # One reaction per person per message: the newest replaces the earlier.
    newest: Dict[Tuple[str, str], Tuple[Tuple[int, int], str, str]] = {}
    for reaction_id, (target, who, emoji, when) in reactions.items():
        key = (target, who)
        if key not in newest or when > newest[key][0]:
            newest[key] = (when, emoji, reaction_id)
    for (target, who), (_when, emoji, reaction_id) in newest.items():
        if who in taken_back.get(reaction_id, ()):   # only its author takes it back
            continue
        bubble = bubbles.get(target)
        if bubble is not None:
            bubble.reactions[who] = emoji
    return [bubbles[i] for i in order]
