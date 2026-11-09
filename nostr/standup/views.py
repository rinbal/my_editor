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

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from . import formats
from .formats import FormatError, is_hex64, is_int, need

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
# Small readers                                                                #
# --------------------------------------------------------------------------- #

def _obj(value: Any, what: str) -> Mapping[str, Any]:
    need(isinstance(value, dict), f"{what} must be an object")
    return value


def _hex(value: Any, what: str) -> str:
    need(is_hex64(value), f"{what} must be 64 hex digits")
    return value


def _opt_hex(value: Any, what: str) -> Optional[str]:
    return None if value is None else _hex(value, what)


def _int(value: Any, what: str, *, low: int = 0) -> int:
    need(is_int(value, low=low), f"{what} must be a whole number of at least {low}")
    return value


def _text(value: Any, what: str, *, limit: int = 2_000) -> str:
    need(isinstance(value, str) and len(value) <= limit, f"{what} must be text")
    return value


def _opt_text(value: Any, what: str, *, limit: int = 2_000) -> Optional[str]:
    return None if value is None else _text(value, what, limit=limit)


def _one_of(value: Any, allowed: Tuple[str, ...], what: str) -> str:
    need(value in allowed, f"{what} is not one of {', '.join(allowed)}")
    return value


def _hex_list(value: Any, what: str, *, limit: int = 1_000) -> Tuple[str, ...]:
    need(isinstance(value, list) and len(value) <= limit, f"{what} must be a list")
    return tuple(_hex(item, what) for item in value)


def _address(value: Any) -> str:
    formats.parse_address(value)
    return value


# --------------------------------------------------------------------------- #
# Views (12.4)                                                                 #
# --------------------------------------------------------------------------- #

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
    published: Optional[Mapping[str, str]]
    placed: bool
    proposal_by: Optional[str]

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


def parse_turn(value: Any) -> Turn:
    data = _obj(value, "the turn")
    return Turn(type=_one_of(data.get("type"), TURN_TYPES, "the turn"),
                pubkey=_opt_hex(data.get("pubkey"), "the turn's person"),
                pending=_hex_list(data.get("pending", []), "the turn's pending",
                                  limit=formats.MAX_PARTICIPANTS + 1))


def parse_view(value: Any) -> SubmissionView:
    data = _obj(value, "a submission")
    state = _one_of(data.get("state"), STATES, "the state")
    step = data.get("step")
    if step is not None:
        _one_of(step, STEPS, "the step")
    confirmations = {}
    raw_confirmations = data.get("confirmations") or {}
    need(isinstance(raw_confirmations, dict) and len(raw_confirmations) <= 64,
         "confirmations must be an object")
    for pubkey, what in raw_confirmations.items():
        _hex(pubkey, "a confirming person")
        need(isinstance(what, list) and all(isinstance(x, str) and len(x) <= 32 for x in what),
             "a confirmation must be a list of words")
        confirmations[pubkey] = tuple(what)
    published = data.get("published")
    if published is not None:
        published = _obj(published, "the publication record")
        published = {"naddr": _text(published.get("naddr"), "the naddr"),
                     "event_id": _hex(published.get("event_id"), "the published event")}
    marks = data.get("marks") or []
    need(isinstance(marks, list), "marks must be a list")
    placed = data.get("placed", False)
    need(isinstance(placed, bool), "placed must be true or false")
    return SubmissionView(
        submission_id=_hex(data.get("submission_id"), "the submission id"),
        publication=_address(data.get("publication")),
        kind=_one_of(data.get("kind"), formats.KINDS, "the kind"),
        title=_text(data.get("title"), "the title", limit=formats.MAX_TITLE_CHARS),
        writer=_hex(data.get("writer"), "the writer"),
        participants=_hex_list(data.get("participants", []), "the participants",
                               limit=formats.MAX_PARTICIPANTS),
        state=state,
        step=step,
        turn=parse_turn(data.get("turn")),
        responsible=_opt_hex(data.get("responsible"), "the responsible editor"),
        # A mark this app does not know (a newer server's) is left out.
        marks=tuple(m for m in marks if m in MARKS),
        signer=_one_of(data.get("signer"), formats.SIGNERS, "who signs"),
        section=_opt_text(data.get("section"), "the section", limit=300),
        revision=_int(data.get("revision"), "the revision", low=1),
        round=_int(data.get("round"), "the round", low=1),
        p_sub=_hex(data.get("p_sub"), "the submission key"),
        epoch=_int(data.get("epoch"), "the epoch"),
        revision_hash=_hex(data.get("revision_hash"), "the revision hash"),
        created_at=_int(data.get("created_at"), "created_at"),
        updated_at=_int(data.get("updated_at"), "updated_at"),
        last_seq=_int(data.get("last_seq", 0), "last_seq"),
        confirmations=confirmations,
        published=published,
        placed=placed,
        proposal_by=_opt_hex(data.get("proposal_by"), "the proposer"),
    )


@dataclass(frozen=True)
class SignedRecord:
    """A stored signed action: the NIP-98 event and the exact body."""

    event: Mapping[str, Any]
    body: str


def parse_signed(value: Any) -> Optional[SignedRecord]:
    if value is None:
        return None
    data = _obj(value, "a signed action")
    event = _obj(data.get("event"), "a signed action's event")
    return SignedRecord(event=event, body=_text(data.get("body"), "a signed body",
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
    seq: int
    at: int
    type: str
    wrap: Optional[Mapping[str, Any]]
    event: Optional[TimelineEvent]


def parse_item(value: Any) -> Optional[TimelineItem]:
    """One timeline item, or None for an event this app does not know."""
    data = _obj(value, "a timeline item")
    kind = _one_of(data.get("type"), ITEM_TYPES, "the item's type")
    wrap = data.get("wrap")
    if wrap is not None:
        wrap = _obj(wrap, "a wrapped message")
    event = data.get("event")
    if event is not None:
        event = _obj(event, "a timeline event")
        if event.get("name") not in EVENT_NAMES:
            return None
        revision = event.get("revision")
        event = TimelineEvent(
            name=event["name"],
            actor=_opt_hex(event.get("actor"), "the actor"),
            revision=None if revision is None else _int(revision, "the revision", low=1),
            to=_opt_hex(event.get("to"), "to whom"),
            signed=parse_signed(event.get("signed")))
    need(kind == "event" or wrap is not None, "a message without its wrap")
    need(kind != "event" or event is not None, "an event without its record")
    return TimelineItem(seq=_int(data.get("seq"), "seq", low=1),
                        at=_int(data.get("at"), "at"), type=kind, wrap=wrap, event=event)


def parse_timeline(value: Any, *, internal: bool) -> Tuple[TimelineItem, ...]:
    """The items, in ``seq`` order, each once. Internal notes only with
    ``internal`` (a member's own view): the writer's side never shows
    one, whatever a server sends (criterion 8 of the plan)."""
    need(isinstance(value, list), "the timeline must be a list")
    items: Dict[int, TimelineItem] = {}
    for raw in value:
        item = parse_item(raw)
        if item is None or (item.type == "internal" and not internal):
            continue
        items.setdefault(item.seq, item)
    return tuple(items[seq] for seq in sorted(items))


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


def parse_receipt(value: Any) -> Receipt:
    data = _obj(value, "a receipt")
    need(data.get("v") == formats.CONTRACT_VERSION and is_int(data.get("v")),
         "the receipt is of another version")
    return Receipt(raw=dict(data),
                   submission_id=_hex(data.get("submission_id"), "the receipt's submission"),
                   revision=_int(data.get("revision"), "the receipt's revision", low=1),
                   revision_hash=_hex(data.get("revision_hash"), "the receipt's revision hash"),
                   blobs_missing=_hex_list(data.get("blobs_missing", []), "missing files"),
                   state=_one_of(data.get("state"), STATES, "the receipt's state"),
                   seq=_int(data.get("seq"), "the receipt's seq", low=1),
                   received_at=_int(data.get("received_at"), "received_at", low=1))


@dataclass(frozen=True)
class LookupItem:
    status_key_hash: str
    submission: SubmissionView
    receipt: Receipt
    timeline: Tuple[TimelineItem, ...]


def parse_lookup_item(value: Any) -> LookupItem:
    data = _obj(value, "a lookup answer")
    view = parse_view(data.get("submission"))
    receipt = parse_receipt(data.get("receipt"))
    need(receipt.submission_id == view.submission_id,
         "the receipt is for another submission")
    return LookupItem(status_key_hash=_hex(data.get("status_key_hash"), "the status key hash"),
                      submission=view, receipt=receipt,
                      timeline=parse_timeline(data.get("timeline", []), internal=False))


@dataclass(frozen=True)
class RevisionRecord:
    revision: int
    revision_hash: str
    envelope: Optional[str]
    envelope_sha256: Optional[str]
    wraps: Mapping[str, Mapping[str, str]]
    blobs: Tuple[Mapping[str, Any], ...]
    created_at: int
    signed: Optional[SignedRecord]


def parse_revision_record(value: Any) -> RevisionRecord:
    data = _obj(value, "a revision")
    wraps = _obj(data.get("wraps", {}), "the wraps")
    for name, wrap in wraps.items():
        need(isinstance(name, str) and formats.is_wrap(wrap), "a wrap is malformed")
    blobs = data.get("blobs") or []
    need(isinstance(blobs, list), "the files must be a list")
    for blob in blobs:
        blob = _obj(blob, "a file")
        _hex(blob.get("sha256"), "a file's hash")
    envelope = data.get("envelope")
    need(envelope is None or (isinstance(envelope, str)
                              and len(envelope) <= formats.MAX_ENVELOPE_BYTES * 4 // 3 + 4),
         "the envelope must be base64 text")
    return RevisionRecord(
        revision=_int(data.get("revision"), "the revision", low=1),
        revision_hash=_hex(data.get("revision_hash"), "the revision hash"),
        envelope=envelope,
        envelope_sha256=_opt_hex(data.get("envelope_sha256"), "the envelope hash"),
        wraps=dict(wraps), blobs=tuple(blobs),
        created_at=_int(data.get("created_at"), "created_at"),
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


def lookup_confirms(receipt: Receipt, *, submission_id: str, revision: int,
                    revision_hash: str) -> bool:
    """True when a looked-up receipt is for exactly this revision."""
    return (receipt.submission_id == submission_id and receipt.revision == revision
            and receipt.revision_hash == revision_hash)


# --------------------------------------------------------------------------- #
# Signed actions (10)                                                          #
# --------------------------------------------------------------------------- #

def action_path(name: str, submission_id: str) -> Optional[str]:
    template = ACTION_PATHS.get(name)
    return template.format(sid=submission_id) if template else None


def signed_action_problem(signed: Optional[SignedRecord], *, path: str, submission_id: str,
                          method: str = "POST",
                          signers: Optional[Iterable[str]] = None) -> Optional[str]:
    """Why a stored signed action cannot be shown as confirmed, or None.

    Contract 10: a valid kind 27235 signature; one ``payload`` tag equal
    to SHA-256 of the stored body; the URL's path is the action's (the
    host may differ behind a proxy); the method; the body's
    ``submission_id``; and, when ``signers`` is given, a signer who holds
    the role shown (the member list comes from the server)."""
    if signed is None:
        return "nothing signed is stored"
    event = signed.event
    if not formats.is_signed_event(event, kind=27235):
        return "the signature does not verify"
    payloads = formats.tag_values(event, "payload")
    if payloads != [formats.sha256_hex(signed.body)]:
        return "the signature covers another body"
    methods = formats.tag_values(event, "method")
    if len(methods) != 1 or methods[0].upper() != method.upper():
        return "the signature is for another method"
    urls = formats.tag_values(event, "u")
    try:
        signed_path = urlsplit(urls[0]).path if len(urls) == 1 else None
    except ValueError:
        signed_path = None
    if signed_path != path:
        return "the signature is for another action"
    try:
        body = formats.strict_json(signed.body, limit=_MAX_SIGNED_BODY)
    except FormatError:
        return "the stored body is not JSON"
    if not isinstance(body, dict) or body.get("submission_id") != submission_id:
        return "the stored body is for another submission"
    if signers is not None and event["pubkey"] not in set(signers):
        return "signed by someone without that role"
    return None


__all__ = [
    "ACTION_PATHS", "BlobsMissing", "Confirmed", "EVENT_NAMES", "ITEM_TYPES", "KIND_HINTS",
    "LookupItem", "MARKS", "MEMBER_ROLES", "Mismatch", "OPEN_STATES", "PARTICIPANT_ROLES",
    "RECEIPT_CLOCK_SKEW", "Receipt", "RevisionRecord", "STATES", "STEPS", "SignedRecord",
    "SubmissionView", "TURN_TYPES", "TimelineEvent", "TimelineItem", "Turn",
    "action_path", "check_receipt", "expected_receipt", "lookup_confirms", "parse_item",
    "parse_lookup_item", "parse_receipt", "parse_revision_record", "parse_signed",
    "parse_timeline", "parse_turn", "parse_view", "signed_action_problem",
]
