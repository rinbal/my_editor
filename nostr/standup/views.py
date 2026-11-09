# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the STANDUP server answers, read defensively, and the checks an
app makes before it believes it (editorial contract 10, 11, 12.4).

- :func:`check_receipt` compares every field of a receipt with the body
  that was sent and says :class:`Confirmed`, :class:`BlobsMissing` or
  :class:`Mismatch`. Only Confirmed may show as "Eingereicht" (criterion
  2 of the plan): a server that stored something else, or not the files,
  has not received this submission.
- :func:`signed_action_problem` checks a stored signed action (signature,
  payload hash, path, submission) before its result is shown; an action
  that does not pass shows as "nicht bestätigt".
- The views (:class:`SubmissionView`, :class:`TimelineItem`,
  :class:`Receipt`, :class:`LookupItem`, :class:`RevisionRecord`) parse
  the server's JSON and raise FormatError for anything malformed.

Pure: no Qt, no network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .. import events as nostr_events
from . import formats
from .formats import FormatError, is_hex64, is_int, need
from .readers import (
    read_address,
    read_hex64,
    read_hex64_list,
    read_int,
    read_object,
    read_one_of,
    read_optional_hex64,
    read_optional_text,
    read_text,
)

# 11.1
STATES = ("submitted", "in_review", "changes_requested", "accepted", "published",
          "declined", "withdrawn")
OPEN_STATES = ("submitted", "in_review", "changes_requested")
STEPS = ("confirm", "writer_publishes", "ready_for_release")
MARKS = ("already_public", "new_revision", "changed_since_acceptance", "proposal")
# 11.2
TURN_TYPES = ("writer", "editor", "redaktion", "contributors", "releaser", "none")
# 12.4
ITEM_TYPES = ("message", "internal", "event")
CHANNELS = ("conversation", "internal")
EVENT_NAMES = ("submitted", "revision", "taken", "handed_over", "handed_back", "named",
               "request_changes", "accept", "decline", "reopen", "withdrawn", "confirmed",
               "published", "placed", "correction", "reminder", "reupload_requested")
MEMBER_ROLES = ("owner", "redaktion", "curator")
PARTICIPANT_ROLES = ("writer", "contributor")
KIND_HINTS = ("message", "reaction", "retraction", "edit")

# A receipt's received_at may be this far ahead of this computer's clock.
RECEIPT_CLOCK_SKEW = 600

# Where each signed action of the timeline was sent (12.4), for
# :func:`signed_action_problem`. ``{sid}`` is the submission.
ACTION_PATHS = {
    "submitted": "/api/submissions",
    "revision": "/api/submissions",
    "taken": "/api/submissions/{sid}/responsible",
    "handed_over": "/api/submissions/{sid}/responsible",
    "handed_back": "/api/submissions/{sid}/responsible",
    "request_changes": "/api/submissions/{sid}/decision",
    "accept": "/api/submissions/{sid}/decision",
    "decline": "/api/submissions/{sid}/decision",
    "reopen": "/api/submissions/{sid}/decision",
    "withdrawn": "/api/submissions/{sid}/withdraw",
    "confirmed": "/api/submissions/{sid}/confirmation",
    "published": "/api/submissions/{sid}/published",
    "placed": "/api/submissions/{sid}/placed",
    "correction": "/api/submissions/{sid}/corrections",
    "reminder": "/api/submissions/{sid}/remind",
    "reupload_requested": "/api/submissions/{sid}/reupload",
}

_MAX_SIGNED_BODY = formats.MAX_ENVELOPE_BYTES * 2


# --------------------------------------------------------------------------- #
# Views (12.4)                                                                 #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Published:
    """Where the published work is: its ``naddr`` and event id."""

    naddr: str
    event_id: str


@dataclass(frozen=True)
class Turn:
    type: str
    pubkey: Optional[str]
    pending: Tuple[str, ...]


@dataclass(frozen=True)
class SubmissionView:
    submission_id: str
    publication: str
    kind: str
    title: str
    writer: str
    participants: Tuple[str, ...]
    state: str
    step: Optional[str]
    turn: Turn
    responsible: Optional[str]
    marks: Tuple[str, ...]
    signer: str
    section: Optional[str]
    revision: int
    round: int
    p_sub: str
    epoch: int
    revision_hash: str
    created_at: int
    updated_at: int
    last_seq: int
    confirmations: Mapping[str, Tuple[str, ...]]
    published: Optional[Published]
    placed: bool
    proposal_by: Optional[str]
    # The newest message or event (9.3: reactions, retractions and edits
    # take a seq, but never make a row unread); for members also the
    # newest internal note's internal_seq (None in a participant's View).
    last_message_seq: int = 0
    last_internal_message_seq: Optional[int] = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    def unread(self, seen: int, internal_seen: int = 0) -> bool:
        """Whether something to read came after ``seen`` (the read state's
        seq for this sid) or ``internal_seen`` (its ``i:<sid>``): from
        ``last_message_seq``, never from ``last_seq`` (contract 9.3)."""
        if self.last_message_seq > seen:
            return True
        return (self.last_internal_message_seq or 0) > internal_seen


def parse_turn(value: Any) -> Turn:
    data = read_object(value, "the turn")
    return Turn(type=read_one_of(data.get("type"), TURN_TYPES, "the turn"),
                pubkey=read_optional_hex64(data.get("pubkey"), "the turn's person"),
                pending=read_hex64_list(data.get("pending", []), "the turn's pending",
                                        limit=formats.MAX_PARTICIPANTS + 1))


def parse_view(value: Any) -> SubmissionView:
    data = read_object(value, "a submission")
    state = read_one_of(data.get("state"), STATES, "the state")
    step = data.get("step")
    if step is not None:
        read_one_of(step, STEPS, "the step")
    confirmations = {}
    raw_confirmations = data.get("confirmations") or {}
    need(isinstance(raw_confirmations, dict) and len(raw_confirmations) <= 64,
         "confirmations must be an object")
    for pubkey, what in raw_confirmations.items():
        read_hex64(pubkey, "a confirming person")
        need(isinstance(what, list) and all(isinstance(x, str) and len(x) <= 32 for x in what),
             "a confirmation must be a list of words")
        confirmations[pubkey] = tuple(what)
    published = data.get("published")
    if published is not None:
        published = read_object(published, "the publication record")
        published = Published(naddr=read_text(published.get("naddr"), "the naddr"),
                              event_id=read_hex64(published.get("event_id"),
                                                  "the published event"))
    marks = data.get("marks") or []
    need(isinstance(marks, list), "marks must be a list")
    placed = data.get("placed", False)
    need(isinstance(placed, bool), "placed must be true or false")
    return SubmissionView(
        submission_id=read_hex64(data.get("submission_id"), "the submission id"),
        publication=read_address(data.get("publication")),
        kind=read_one_of(data.get("kind"), formats.KINDS, "the kind"),
        title=read_text(data.get("title"), "the title", limit=formats.MAX_TITLE_CHARS),
        writer=read_hex64(data.get("writer"), "the writer"),
        participants=read_hex64_list(data.get("participants", []), "the participants",
                                     limit=formats.MAX_PARTICIPANTS),
        state=state,
        step=step,
        turn=parse_turn(data.get("turn")),
        responsible=read_optional_hex64(data.get("responsible"), "the responsible editor"),
        # A mark this app does not know (a newer server's) is left out.
        marks=tuple(m for m in marks if m in MARKS),
        signer=read_one_of(data.get("signer"), formats.SIGNERS, "who signs"),
        section=read_optional_text(data.get("section"), "the section", limit=300),
        revision=read_int(data.get("revision"), "the revision", low=1),
        round=read_int(data.get("round"), "the round", low=1),
        p_sub=read_hex64(data.get("p_sub"), "the submission key"),
        epoch=read_int(data.get("epoch"), "the epoch"),
        revision_hash=read_hex64(data.get("revision_hash"), "the revision hash"),
        created_at=read_int(data.get("created_at"), "created_at"),
        updated_at=read_int(data.get("updated_at"), "updated_at"),
        last_seq=read_int(data.get("last_seq", 0), "last_seq"),
        last_message_seq=read_int(data.get("last_message_seq", 0), "last_message_seq"),
        last_internal_message_seq=None if data.get("last_internal_message_seq") is None
        else read_int(data["last_internal_message_seq"], "last_internal_message_seq"),
        confirmations=confirmations,
        published=published,
        placed=placed,
        proposal_by=read_optional_hex64(data.get("proposal_by"), "the proposer"),
    )


@dataclass(frozen=True)
class SignedRecord:
    """A stored signed action: the NIP-98 event and the exact body."""

    event: Mapping[str, Any]
    body: str


def parse_signed(value: Any) -> Optional[SignedRecord]:
    if value is None:
        return None
    data = read_object(value, "a signed action")
    event = read_object(data.get("event"), "a signed action's event")
    return SignedRecord(event=event, body=read_text(data.get("body"), "a signed body",
                                                    limit=_MAX_SIGNED_BODY))


@dataclass(frozen=True)
class TimelineEvent:
    name: str
    actor: Optional[str]
    revision: Optional[int]
    to: Optional[str]
    signed: Optional[SignedRecord]


@dataclass(frozen=True)
class TimelineItem:
    """One item of a channel: ``seq`` counts in that channel only (an
    internal note's is its ``internal_seq``, contract 12.4)."""

    seq: int
    at: int
    type: str
    wrap: Optional[Mapping[str, Any]]
    event: Optional[TimelineEvent]

    @property
    def channel(self) -> str:
        return "internal" if self.type == "internal" else "conversation"


def parse_item(value: Any) -> Optional[TimelineItem]:
    """One timeline item, or None for an event this app does not know."""
    data = read_object(value, "a timeline item")
    kind = read_one_of(data.get("type"), ITEM_TYPES, "the item's type")
    wrap = data.get("wrap")
    if wrap is not None:
        wrap = read_object(wrap, "a wrapped message")
    event = data.get("event")
    if event is not None:
        event = read_object(event, "a timeline event")
        if event.get("name") not in EVENT_NAMES:
            return None
        revision = event.get("revision")
        event = TimelineEvent(
            name=event["name"],
            actor=read_optional_hex64(event.get("actor"), "the actor"),
            revision=None if revision is None else read_int(revision, "the revision", low=1),
            to=read_optional_hex64(event.get("to"), "to whom"),
            signed=parse_signed(event.get("signed")))
    need(kind == "event" or wrap is not None, "a message without its wrap")
    need(kind != "event" or event is not None, "an event without its record")
    return TimelineItem(seq=read_int(data.get("seq"), "seq", low=1),
                        at=read_int(data.get("at"), "at"), type=kind, wrap=wrap, event=event)


def parse_channels(value: Any) -> Dict[str, Tuple[TimelineItem, ...]]:
    """The items of each channel (``conversation``, ``internal``), each in
    its own ``seq`` order and each (channel, ``seq``) once: the channels
    count apart, so internal note 3 is not message 3 (contract 12.4,
    feat/redaktion ceac858)."""
    need(isinstance(value, list), "the timeline must be a list")
    items: Dict[Tuple[str, int], TimelineItem] = {}
    for raw in value:
        item = parse_item(raw)
        if item is not None:
            items.setdefault((item.channel, item.seq), item)
    return {channel: tuple(items[key] for key in sorted(items) if key[0] == channel)
            for channel in CHANNELS}


def parse_timeline(value: Any) -> Tuple[TimelineItem, ...]:
    """The conversation's items, in ``seq`` order, each once: a lookup's
    timeline, which never holds internal notes (contract 12.4); one a
    server sends anyway is left out, never shown on the writer's side."""
    return parse_channels(value)["conversation"]


@dataclass(frozen=True)
class Receipt:
    """A receipt as the server gave it (12.4). Whether it matches what was
    sent is :func:`check_receipt`'s question."""

    raw: Mapping[str, Any]
    submission_id: str
    revision: int
    revision_hash: str
    blobs_missing: Tuple[str, ...]
    state: str
    seq: int
    received_at: int
    # What the server stored: the envelope's hash (None for a work by
    # link) and each wrap's, by name.
    envelope_sha256: Optional[str]
    wraps_sha256: Mapping[str, str] = field(hash=False)


def _read_wrap_hashes(value: Any) -> Dict[str, str]:
    data = read_object(value, "the wraps' hashes")
    need(len(data) <= formats.MAX_PARTICIPANTS + 2, "too many wraps")
    for name, digest in data.items():
        need(isinstance(name, str) and 0 < len(name) <= 64, "a wrap's name")
        read_hex64(digest, "a wrap's hash")
    return dict(data)


def parse_receipt(value: Any) -> Receipt:
    data = read_object(value, "a receipt")
    need(data.get("v") == formats.CONTRACT_VERSION and is_int(data.get("v")),
         "the receipt is of another version")
    return Receipt(raw=dict(data),
                   submission_id=read_hex64(data.get("submission_id"), "the receipt's submission"),
                   revision=read_int(data.get("revision"), "the receipt's revision", low=1),
                   revision_hash=read_hex64(data.get("revision_hash"), "the receipt's revision hash"),
                   blobs_missing=read_hex64_list(data.get("blobs_missing", []), "missing files"),
                   state=read_one_of(data.get("state"), STATES, "the receipt's state"),
                   seq=read_int(data.get("seq"), "the receipt's seq", low=1),
                   received_at=read_int(data.get("received_at"), "received_at", low=1),
                   envelope_sha256=read_optional_hex64(data.get("envelope_sha256"),
                                                       "the receipt's envelope hash"),
                   wraps_sha256=_read_wrap_hashes(data.get("wraps_sha256")))


@dataclass(frozen=True)
class LookupItem:
    status_key_hash: str
    submission: SubmissionView
    receipt: Receipt
    timeline: Tuple[TimelineItem, ...]
    # More timeline items wait: ask again with ``since`` (15, 5c11475).
    more: bool = False


def parse_lookup_item(value: Any) -> LookupItem:
    data = read_object(value, "a lookup answer")
    view = parse_view(data.get("submission"))
    receipt = parse_receipt(data.get("receipt"))
    need(receipt.submission_id == view.submission_id,
         "the receipt is for another submission")
    more = data.get("more", False)
    need(isinstance(more, bool), "more is true or false")
    return LookupItem(status_key_hash=read_hex64(data.get("status_key_hash"), "the status key hash"),
                      submission=view, receipt=receipt,
                      timeline=parse_timeline(data.get("timeline", [])),
                      more=more)


@dataclass(frozen=True)
class Blob:
    """A file of a revision: its hash, size and where it was uploaded."""

    sha256: str
    size: int
    servers: Tuple[str, ...]


def parse_blob(value: Any) -> Blob:
    data = read_object(value, "a file")
    servers = data.get("servers", [])
    need(isinstance(servers, list) and len(servers) <= 20, "a file's servers must be a list")
    return Blob(sha256=read_hex64(data.get("sha256"), "a file's hash"),
                size=read_int(data.get("size", 0), "a file's size"),
                servers=tuple(read_text(s, "a file's server") for s in servers))


@dataclass(frozen=True)
class RevisionRecord:
    revision: int
    revision_hash: str
    envelope: Optional[str]
    envelope_sha256: Optional[str]
    wraps: Mapping[str, Mapping[str, str]]
    blobs: Tuple[Blob, ...]
    created_at: int
    signed: Optional[SignedRecord]


def parse_revision_record(value: Any) -> RevisionRecord:
    data = read_object(value, "a revision")
    wraps = read_object(data.get("wraps", {}), "the wraps")
    for name, wrap in wraps.items():
        need(isinstance(name, str) and formats.is_wrap(wrap), "a wrap is malformed")
    blobs = data.get("blobs") or []
    need(isinstance(blobs, list), "the files must be a list")
    envelope = data.get("envelope")
    need(envelope is None or (isinstance(envelope, str)
                              and len(envelope) <= formats.MAX_ENVELOPE_BYTES * 4 // 3 + 4),
         "the envelope must be base64 text")
    return RevisionRecord(
        revision=read_int(data.get("revision"), "the revision", low=1),
        revision_hash=read_hex64(data.get("revision_hash"), "the revision hash"),
        envelope=envelope,
        envelope_sha256=read_optional_hex64(data.get("envelope_sha256"), "the envelope hash"),
        wraps=dict(wraps), blobs=tuple(parse_blob(b) for b in blobs),
        created_at=read_int(data.get("created_at"), "created_at"),
        signed=parse_signed(data.get("signed")))


# --------------------------------------------------------------------------- #
# Receipts (12.4)                                                              #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Confirmed:
    """Every field matches and every file is there: "Eingereicht"."""

    receipt: Receipt
    repeat: bool = False      # the server had it already (200, not 201)


@dataclass(frozen=True)
class BlobsMissing:
    """Stored, but these files were not found where the body says. Upload
    them again and send the same body again (a new signature)."""

    receipt: Receipt
    missing: Tuple[str, ...]


@dataclass(frozen=True)
class Mismatch:
    """The receipt is for something other than what was sent."""

    fields: Tuple[str, ...]
    receipt: Optional[Receipt] = None


ReceiptCheck = Any  # Confirmed | BlobsMissing | Mismatch


def _same(a: Any, b: Any) -> bool:
    """Equal and of the same JSON type all the way down: ``True`` is not
    ``1`` here, as it is in Python."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def expected_receipt(body: Mapping[str, Any]) -> Dict[str, Any]:
    """What a receipt must say for ``body`` (the submission body sent),
    field by field."""
    return {
        "v": formats.CONTRACT_VERSION,
        "submission_id": body["submission_id"],
        "revision": body["revision"],
        "revision_hash": body["revision_hash"],
        # Submit by link sends no envelope (12.4).
        "envelope_sha256": body.get("envelope_sha256"),
        "wraps_sha256": {name: formats.wrap_sha256(wrap)
                         for name, wrap in body.get("wraps", {}).items()},
    }


def _blob_set(value: Any) -> Optional[List[str]]:
    if not isinstance(value, list) or not all(is_hex64(x) for x in value):
        return None
    if len(set(value)) != len(value):
        return None
    return value


def check_receipt(body: Mapping[str, Any], receipt: Any, *, now: float,
                  repeat: bool = False) -> ReceiptCheck:
    """Compare the server's ``receipt`` with the ``body`` that was sent,
    every field of 12.4: version, submission, revision, revision hash,
    envelope hash, the hash of every wrap (each name sent, nothing more),
    the files found and not found (together exactly the files sent),
    ``received_at`` (a time, not ahead of this computer's clock by more
    than :data:`RECEIPT_CLOCK_SKEW`), the state and ``seq``."""
    if not isinstance(receipt, dict):
        return Mismatch(fields=("receipt",))
    wrong: List[str] = []
    for name, value in expected_receipt(body).items():
        got = receipt.get(name)
        if name == "envelope_sha256" and value is None:
            if got is not None:
                wrong.append(name)
            continue
        if not _same(got, value):
            wrong.append(name)

    sent_blobs = [blob["sha256"] for blob in body.get("blobs", [])]
    present = _blob_set(receipt.get("blobs_present", []))
    missing = _blob_set(receipt.get("blobs_missing", []))
    if present is None or missing is None or set(present) & set(missing) \
            or set(present) | set(missing) != set(sent_blobs):
        wrong.append("blobs")
    received_at = receipt.get("received_at")
    if not is_int(received_at, low=1) or received_at > now + RECEIPT_CLOCK_SKEW:
        wrong.append("received_at")
    if receipt.get("state") not in STATES:
        wrong.append("state")
    if not is_int(receipt.get("seq"), low=1):
        wrong.append("seq")
    if wrong:
        parsed = None
        try:
            parsed = parse_receipt(receipt)
        except FormatError:
            pass
        return Mismatch(fields=tuple(wrong), receipt=parsed)

    parsed = parse_receipt(receipt)
    if parsed.blobs_missing:
        return BlobsMissing(receipt=parsed, missing=parsed.blobs_missing)
    return Confirmed(receipt=parsed, repeat=repeat)


def same_revision(receipt: Receipt, *, submission_id: str, revision: int,
                  revision_hash: str) -> bool:
    """Whether a looked-up receipt is of this revision: its number and
    hash. Confirmed only when the envelope and the wraps are the ones
    sent as well (``entries.same_sent``, 12.4 "No answer after
    sending")."""
    return (receipt.submission_id == submission_id and receipt.revision == revision
            and receipt.revision_hash == revision_hash)


# --------------------------------------------------------------------------- #
# Signed actions (10)                                                          #
# --------------------------------------------------------------------------- #

def action_path(name: str, submission_id: str) -> Optional[str]:
    template = ACTION_PATHS.get(name)
    return template.format(sid=submission_id) if template else None


class SignedActionInvalid(ValueError):
    """Why a stored signed action does not count (:func:`signed_action_body`)."""


def signed_action_body(signed: Optional[SignedRecord], *, path: str, submission_id: str,
                       method: str = "POST",
                       signers: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """The body of a stored signed action, once it is verified; raises
    :class:`SignedActionInvalid` with the reason otherwise.

    Contract 10: a valid kind 27235 signature; one ``payload`` tag equal
    to SHA-256 of the stored body; the URL's path is the action's (the
    host may differ behind a proxy); the method; the body's
    ``submission_id``; and, when ``signers`` is given, a signer who holds
    the role shown (the member list comes from the server)."""
    if signed is None:
        raise SignedActionInvalid("nothing signed is stored")
    event = signed.event
    if not nostr_events.is_verified_event(event, kind=27235):
        raise SignedActionInvalid("the signature does not verify")
    payloads = formats.tag_values(event, "payload")
    if payloads != [formats.sha256_hex(signed.body)]:
        raise SignedActionInvalid("the signature covers another body")
    methods = formats.tag_values(event, "method")
    if len(methods) != 1 or methods[0].upper() != method.upper():
        raise SignedActionInvalid("the signature is for another method")
    if signed_path(signed) != path:
        raise SignedActionInvalid("the signature is for another action")
    try:
        body = formats.strict_json(signed.body, limit=_MAX_SIGNED_BODY)
    except FormatError:
        raise SignedActionInvalid("the stored body is not JSON") from None
    if not isinstance(body, dict) or body.get("submission_id") != submission_id:
        raise SignedActionInvalid("the stored body is for another submission")
    if signers is not None and event["pubkey"] not in set(signers):
        raise SignedActionInvalid("signed by someone without that role")
    return body


def signed_action_problem(signed: Optional[SignedRecord], *, path: str, submission_id: str,
                          method: str = "POST",
                          signers: Optional[Iterable[str]] = None) -> Optional[str]:
    """Why a stored signed action cannot be shown as confirmed, or None
    (:func:`signed_action_body`)."""
    try:
        signed_action_body(signed, path=path, submission_id=submission_id, method=method,
                           signers=signers)
    except SignedActionInvalid as exc:
        return str(exc)
    return None


def signed_path(signed: SignedRecord) -> Optional[str]:
    """The path of the one URL a signed action was signed for, or None."""
    urls = formats.tag_values(signed.event, "u")
    try:
        return urlsplit(urls[0]).path if len(urls) == 1 else None
    except ValueError:
        return None


__all__ = [
    "ACTION_PATHS", "BlobsMissing", "CHANNELS", "Confirmed", "EVENT_NAMES", "ITEM_TYPES",
    "KIND_HINTS", "LookupItem", "MARKS", "MEMBER_ROLES", "Mismatch", "OPEN_STATES",
    "PARTICIPANT_ROLES", "RECEIPT_CLOCK_SKEW", "Receipt", "RevisionRecord", "STATES", "STEPS",
    "SignedRecord", "SubmissionView", "TURN_TYPES", "TimelineEvent", "TimelineItem", "Turn",
    "action_path", "check_receipt", "expected_receipt", "same_revision", "parse_item",
    "parse_lookup_item", "parse_receipt", "parse_revision_record", "parse_signed",
    "parse_channels", "parse_timeline", "parse_turn", "parse_view", "SignedActionInvalid",
    "signed_action_body", "signed_action_problem", "signed_path",
]
