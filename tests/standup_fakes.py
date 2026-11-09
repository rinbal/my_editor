# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fakes for the STANDUP submission tests.

:class:`KeySigner` is a signer with the calls of LocalSigner and
BunkerClient (``sign_event``, ``nip44_encrypt``/``nip44_decrypt`` and
their ``_self`` forms) that answers at once, with a real key, so what it
signs verifies and what it encrypts decrypts.

:class:`FakeStandupServer` is a STANDUP server that checks; see the
comment before it.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import json
import re
import secrets
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from coincurve import PublicKeyXOnly
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QNetworkReply

from nostr import crypto, events, nip98
from nostr.standup import crypto as scrypto
from nostr.standup import formats
from tests.membership_fakes import FakeNam, FakeReply, header, json_reply, transport_failure
from tests.standup_vectors import VECTORS


class KeySigner:
    """Signs and encrypts with ``secret`` and answers inside the call.

    ``refuse`` refuses every request with that reason; ``tamper`` edits a
    signed event before it is handed back. ``signed`` and ``asked`` record
    what was requested."""

    def __init__(self, secret: bytes, *, refuse: Optional[str] = None,
                 tamper: Optional[Callable[[dict], dict]] = None) -> None:
        self.secret = bytes(secret)
        self.user_pubkey = crypto.get_public_key(self.secret).hex()
        self.refuse = refuse
        self.tamper = tamper
        self.signed: List[dict] = []
        self.asked: List[str] = []
        self.is_connected = True

    def sign_event(self, unsigned: dict, on_success, on_failure, *, timeout_ms: int = 0) -> None:
        self.asked.append(f"sign_event:{unsigned.get('kind')}")
        if self.refuse is not None:
            on_failure(self.refuse)
            return
        event = events.sign_event(json.loads(json.dumps(unsigned)), self.secret)
        if self.tamper is not None:
            event = self.tamper(event)
        self.signed.append(event)
        on_success(event)

    def nip44_encrypt(self, peer: str, plaintext: str, on_success, on_failure, *,
                      timeout_ms: int = 0) -> None:
        self.asked.append("nip44_encrypt")
        if self.refuse is not None:
            on_failure(self.refuse)
            return
        on_success(crypto.encrypt_to(plaintext, self.secret, bytes.fromhex(peer)))

    def nip44_decrypt(self, peer: str, ciphertext: str, on_success, on_failure, *,
                      timeout_ms: int = 0) -> None:
        self.asked.append("nip44_decrypt")
        if self.refuse is not None:
            on_failure(self.refuse)
            return
        try:
            text = crypto.decrypt_from(ciphertext, self.secret, bytes.fromhex(peer))
        except ValueError as exc:
            on_failure(str(exc))
            return
        on_success(text)

    def nip44_encrypt_self(self, plaintext: str, on_success, on_failure, *,
                           timeout_ms: int = 0) -> None:
        self.nip44_encrypt(self.user_pubkey, plaintext, on_success, on_failure)

    def nip44_decrypt_self(self, ciphertext: str, on_success, on_failure, *,
                           timeout_ms: int = 0) -> None:
        self.nip44_decrypt(self.user_pubkey, ciphertext, on_success, on_failure)


# =========================================================================== #
# A STANDUP server that checks                                                #
# =========================================================================== #
#
# FakeStandupServer answers the requests of nostr/standup/client.py the
# way editorial contract 12 says, through the FakeNam transport of the
# membership tests. Every signed action is checked like the real server
# checks it (nostr.nip98.check_auth_header: kind, method, the exact URL,
# the 60 second window, the payload hash, lowercase hex, the signature),
# plus the two rules the contract adds: a payload tag on every signed
# action, and each event id only once. Sessions are tokens of 32 random
# bytes of which only SHA-256 is kept, as on the server. Submissions move
# through the states of 11.3 (``transition``, tested on the fixture's
# transition table), with receipts, idempotency and the limits.
#
# Faults are injected per route (``inject``): the connection dropped
# before the server saw the request, dropped after it stored it (no
# answer), or a given refusal; ``tamper_receipt`` changes what a receipt
# says, ``blobs_present`` decides which files a HEAD request finds.

SERVER = "https://standup.example"     # the fixture key event's "server"
KEY_EVENT = VECTORS["key_event"]["valid"]
ADDRESS = VECTORS["key_event"]["address"]
SESSION_SECONDS = 12 * 3600

OPEN = ("submitted", "in_review", "changes_requested")
ROUND_CLOSED = ("declined", "withdrawn")
REDAKTION = ("owner", "redaktion")

# Contract 15 (feat/redaktion 5c11475).
MAX_REVISIONS = 30
SUBMISSION_WRAP_CT_CHARS = 348
# The backend's numbers (feat/redaktion-backend fde04a3): timeline items
# per response (core/editorial_views.py), per lookup item and per GET,
# and the lookup's own limits (api/submission_reads.py).
MAX_TIMELINE_BYTES = 8 * 1024 * 1024
LOOKUP_TIMELINE = 100
TIMELINE_PAGE = 500
LOOKUP_BODY_BYTES = 16 * 1024
LOOKUPS_PER_ADDRESS_PER_MINUTE = 30
# api/submissions.py, core/editorial_submission.py, core/editorial_intake.py,
# core/editorial_quota.py and core/editorial_trust.py.
SUBMIT_REQUESTS_PER_HOUR = 60
SUBMIT_REQUESTS_PER_ADDRESS_PER_HOUR = 120
REVISIONS_PER_HOUR = 10
STORED_BYTES_PER_DAY = 64 * 1024 * 1024
MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
SUBMIT_BODY_BYTES = (MAX_ENVELOPE_BYTES + 2) // 3 * 4 + 1024 * 1024 + 512 * 1024
ENVELOPE_OVERHEAD = 12 + 16
SUBMIT_FIELDS = {"v", "submission_id", "round", "revision", "revision_hash", "publication",
                 "key_event_id", "epoch", "kind", "title", "coordinate_hash", "signer",
                 "participants", "p_sub", "wraps", "status_key_hash", "client"}
ENVELOPE_FIELDS = {"envelope", "envelope_sha256", "blobs"}
MAX_SAFE_INTEGER = 2 ** 53 - 1
# Events served once with their Revision, never again in the timeline.
REVISION_EVENTS = ("submitted", "revision")

DROP_BEFORE = "drop before"     # the connection fails before the request arrives
DROP_AFTER = "drop after"       # the request is handled, the answer is lost


@dataclass
class Fault:
    """A refusal to answer instead of handling the request."""

    status: int
    code: str
    retry_after: Optional[int] = None
    message: str = "Nicht möglich."
    detail: Optional[dict] = None


def transition(state: str, action: str, role: str) -> Optional[str]:
    """The state after ``action`` by someone in ``role``, or None when it
    is refused (contract 11.3)."""
    redaktion = role in REDAKTION
    if action == "revise":
        if role != "writer":
            return None
        return {"submitted": "submitted", "in_review": "in_review",
                "changes_requested": "in_review"}.get(state)
    if action == "withdraw":
        return "withdrawn" if role == "writer" and state in OPEN else None
    if action == "decline":
        return "declined" if redaktion and state in OPEN else None
    if action == "request_changes":
        return "changes_requested" if redaktion and state in ("submitted", "in_review") else None
    if action == "request_changes_without_message":
        return None
    if action == "accept":
        return "accepted" if redaktion and state in ("submitted", "in_review") else None
    if action == "accept_public":
        # A work already public (by link, a proposal) is published at once
        # (11.3, feat/redaktion 5c11475).
        return "published" if redaktion and state in ("submitted", "in_review") else None
    if action == "reopen":
        return "in_review" if redaktion and state == "declined" else None
    if action == "take":
        if not redaktion or state not in OPEN:
            return None
        return "in_review" if state == "submitted" else state
    if action == "message":
        # The first visible act of the editorial team moves it to review;
        # a curator's or a writer's message changes nothing.
        return "in_review" if redaktion and state == "submitted" else state
    if action in ("internal_note", "react"):
        return state
    if action == "published_by_writer":
        return "published" if role == "writer" and state == "accepted" else None
    if action == "published_by_release":
        return "published" if role == "releaser" and state == "accepted" else None
    if action == "placed":
        return "published" if role == "releaser" and state == "published" else None
    return None


def _strict(raw: bytes) -> Any:
    """JSON with no key twice, as the server reads it."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("a key twice")
            result[key] = value
        return result
    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs)


def _signed(event: Any, kind: int) -> bool:
    return (isinstance(event, dict) and type(event.get("kind")) is int
            and event["kind"] == kind and events.verify_event(event))


def _error(status: int, code: str, message: str = "Nicht möglich.", *,
           detail: Optional[dict] = None, headers: Optional[dict] = None) -> FakeReply:
    error = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return json_reply({"ok": False, "error": error}, status=status, headers=headers)


def _ok(payload: dict, *, status: int = 200) -> FakeReply:
    return json_reply(dict({"ok": True}, **payload), status=status)


class StandupNam(FakeNam):
    """FakeNam with the custom verbs (PUT, PATCH) Qt sends through
    ``sendCustomRequest``."""

    def sendCustomRequest(self, request, verb, data=None):
        verb = bytes(verb).decode("ascii")
        return self._issue(verb, request, bytes(data) if data is not None else None)

    def settle(self, *, limit: int = 50) -> None:
        """Finish every reply, and every answer the client gives on a
        later turn (a refusal before sending) with whatever it starts."""
        for _ in range(limit):
            QCoreApplication.processEvents()
            if not [r for r in self.issued if not r.settled]:
                QCoreApplication.processEvents()
                if not [r for r in self.issued if not r.settled]:
                    return
            super().settle(limit=limit)
        raise AssertionError("fake transport did not settle")


@dataclass
class Publication:
    address: str
    key_event: dict
    open: bool = True
    paused: bool = False
    blocked: set = dc_field(default_factory=set)
    # pubkey -> role ("owner", "redaktion", "curator")
    members: Dict[str, str] = dc_field(default_factory=dict)
    releasers: set = dc_field(default_factory=set)
    epoch_keys: List[dict] = dc_field(default_factory=list)

    @property
    def content(self) -> dict:
        return json.loads(self.key_event["content"])

    @property
    def epoch(self) -> int:
        return self.content["epoch"]


@dataclass
class Record:
    """One submission as the server keeps it (contract 16: no plaintext)."""

    sid: str
    writer: str
    publication: str
    title: str
    kind: str
    round: int
    signer: str
    participants: List[str]
    p_sub: str
    epoch: int
    status_key_hash: str
    coordinate_hash: str
    created_at: int
    state: str = "submitted"
    step: Optional[str] = None
    responsible: Optional[str] = None
    marks: List[str] = dc_field(default_factory=list)
    section: Optional[str] = None
    revisions: Dict[int, dict] = dc_field(default_factory=dict)
    receipts: Dict[int, dict] = dc_field(default_factory=dict)
    timeline: List[dict] = dc_field(default_factory=list)
    confirmations: Dict[str, List[str]] = dc_field(default_factory=dict)
    published: Optional[dict] = None
    placed: bool = False
    updated_at: int = 0
    seq: int = 0
    last_message_seq: int = 0
    # Internal notes count apart (contract 12.4, feat/redaktion ceac858).
    internal_seq: int = 0
    last_internal_message_seq: int = 0
    activity_at: int = 0

    @property
    def revision(self) -> int:
        return max(self.revisions)

    @property
    def current(self) -> dict:
        return self.revisions[self.revision]



class TimelineBudget:
    """backend core/editorial_views.py TimelineBudget (fde04a3): the bytes
    of timeline items one response may still carry, shared by every item
    of a lookup. An item counts as its compact JSON."""

    def __init__(self, total: int) -> None:
        self.left = total

    def page(self, items: List[dict], *, limit: int) -> Tuple[List[dict], bool]:
        """The first ``limit`` of ``items`` that fit, and ``more``. ``items``
        is read with ``limit + 1``, so one more says there is more."""
        shown = []
        for item in items[:limit]:
            size = len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode())
            if size > self.left:
                return shown, True
            self.left -= size
            shown.append(item)
        return shown, len(items) > limit


@dataclass(frozen=True)
class Caller:
    """Who a request is from: the session's or the signature's person.
    A signed action keeps its event and exact body, as the server stores
    them (10)."""

    pubkey: str
    event: Optional[dict] = None
    body: str = ""

    @property
    def signed(self) -> Optional[dict]:
        return {"event": self.event, "body": self.body} if self.event else None

def _hexes(data: dict, *names: str) -> bool:
    return all(isinstance(data.get(n), str) and re.fullmatch(r"[0-9a-f]{64}", data[n])
               for n in names)


def _fields(data: dict, allowed: set, required: set) -> Optional[FakeReply]:
    """backend core/editorial.py require_keys (fde04a3)."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        return _error(422, "invalid", "Die Anfrage enthält unbekannte Felder.",
                      detail={"fields": unknown})
    missing = sorted(required - set(data))
    if missing:
        return _error(422, "invalid", "Der Anfrage fehlen Felder.", detail={"fields": missing})
    return None


def _plain(value: Any, low: int, high: int) -> bool:
    """backend core/editorial.py is_plain_text: one line, no control characters."""
    return isinstance(value, str) and low <= len(value) <= high and not any(
        ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)


def _nip44_shaped(payload: Any) -> bool:
    """What a server can check of a NIP-44 v2 payload it cannot open:
    canonical base64 and version 2 (backend core/nip44.py)."""
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, TypeError):
        return False
    return base64.b64encode(raw).decode("ascii") == payload and raw[:1] == b"\x02"


class _Refused(Exception):
    """A refusal of the backend, raised where it raises its EditorialError."""

    def __init__(self, status: int, code: str, detail: Optional[dict] = None,
                 message: str = "Nicht möglich.", retry_after: Optional[int] = None) -> None:
        super().__init__(code)
        self.reply = _error(status, code, message, detail=detail, headers=(
            {"Retry-After": str(retry_after)} if retry_after else None))


def _need(ok: bool, field: str, code: str = "invalid", status: int = 422) -> None:
    if not ok:
        raise _Refused(status, code, {"field": field})


def _whole(value: Any, low: int) -> bool:
    return type(value) is int and value >= low


def _on_curve(value: Any) -> bool:
    """An x-only public key on secp256k1, checked with coincurve."""
    if not (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)):
        return False
    try:
        PublicKeyXOnly(bytes.fromhex(value))
    except ValueError:
        return False
    return True


class FakeStandupServer:
    """See the comment above. Use as ``StandupNam(responder=server)``."""

    def __init__(self, clock, *, base: str = SERVER, key_event: Optional[dict] = None) -> None:
        self.clock = clock
        self.base = base
        self.seen_ids: set = set()
        self.refusals: List[str] = []
        self.accepted: List[dict] = []           # verified NIP-98 events
        self.requests: List[Tuple[str, str, Optional[bytes], Dict[str, Optional[bytes]]]] = []
        self.sessions: Dict[str, Tuple[str, int]] = {}   # sha256(token) -> (pubkey, expires)
        self.last_session = ""
        self.keyrings: Dict[str, dict] = {}
        self.submissions: Dict[str, Record] = {}
        self.blobs_present: set = set()
        self.faults: Dict[str, List[Any]] = {}
        self.tamper_receipt: Optional[Callable[[dict], dict]] = None
        self.handled: List[str] = []             # routes that changed or read state
        # Timeline items per answer (contract 15: 500 for GET, 100 per
        # lookup item) and the bytes one answer carries, shared by every
        # lookup item; "more": true when some wait.
        self.timeline_per_answer = {"lookup": LOOKUP_TIMELINE, "get": TIMELINE_PAGE}
        self.timeline_bytes = MAX_TIMELINE_BYTES
        # backend db/editorial.py hit_rate_limit: fixed windows.
        self._counters: Dict[Tuple[str, str, int], int] = {}
        self.bote = {"pubkey": VECTORS["keys"]["bote"]["pubkey"], "npub": "npub1bote",
                     "nip05": "bote@standup.example"}
        self.publications: Dict[str, Publication] = {}
        event = key_event or KEY_EVENT
        content = json.loads(event["content"])
        pub = Publication(address=content["publication"], key_event=event)
        pub.members[content["owner"]] = "owner"
        pub.releasers.add(content["owner"])
        self.publications[pub.address] = pub

    # -- set-up --------------------------------------------------------------

    def inject(self, route: str, fault: Any) -> None:
        """The next request to ``route`` meets ``fault``: DROP_BEFORE,
        DROP_AFTER or a :class:`Fault`."""
        self.faults.setdefault(route, []).append(fault)

    def _hit(self, scope: str, subject: str, *, limit: int, window: int,
             cost: int = 1) -> Optional[int]:
        """backend db/editorial.py hit_rate_limit (fde04a3): count ``cost``
        in a fixed window; the seconds to wait when over ``limit``."""
        now = int(self.clock())
        start = now - now % window
        key = (scope, subject, start)
        self._counters[key] = self._counters.get(key, 0) + cost
        if self._counters[key] > limit:
            return max(1, start + window - now)
        return None

    def expire_sessions(self) -> None:
        for token_hash, (pubkey, _expires) in list(self.sessions.items()):
            self.sessions[token_hash] = (pubkey, int(self.clock()) - 1)

    def publication(self, address: str = ADDRESS) -> Publication:
        return self.publications[address]

    # -- the transport -------------------------------------------------------

    _ROUTES = [
        ("POST", r"/api/session", "session", "signed"),
        ("DELETE", r"/api/session", "session_delete", "session_any"),
        ("GET", r"/api/editorial/instance", "instance", "none"),
        ("GET", r"/api/submissions/destinations", "destinations", "none"),
        ("POST", r"/api/submissions/lookup", "lookup", "none"),
        ("POST", r"/api/submissions", "submit", "signed"),
        ("GET", r"/api/submissions/(?P<sid>[0-9a-f]{64})", "submission_get", "session"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/messages", "messages", "session"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/decision", "decision", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/confirmation", "confirmation", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/responsible", "responsible", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/withdraw", "withdraw", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/published", "published", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/placed", "placed", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/corrections", "corrections", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/remind", "remind", "signed"),
        ("POST", r"/api/submissions/(?P<sid>[0-9a-f]{64})/reupload", "reupload", "signed"),
        ("GET", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/settings", "settings_get", "none"),
        ("PUT", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/settings", "settings_put", "signed"),
        ("GET", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/members", "members_get", "session"),
        ("POST", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/members", "members_post", "signed"),
        ("POST", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/members/accept", "members_accept", "signed"),
        ("POST", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/members/remove", "members_remove", "signed"),
        ("PATCH", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/members/me", "members_me", "signed"),
        ("POST", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/epochs", "epochs", "signed"),
        ("GET", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/keys", "keys", "session"),
        ("POST", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/proposals", "proposals", "signed"),
        ("GET", r"/api/sites/(?P<site>by/[0-9a-f]{64}/[A-Za-z0-9_-]+)/submissions", "site_submissions", "session"),
        ("GET", r"/api/me/keyring", "keyring_get", "session"),
        ("PUT", r"/api/me/keyring", "keyring_put", "signed"),
        ("GET", r"/api/me/editorial", "me_editorial", "session"),
        ("GET", r"/api/me/submissions", "me_submissions", "session"),
        ("GET", r"/\.well-known/nostr\.json", "nip05", "none"),
    ]

    def route_of(self, verb: str, path: str) -> Tuple[Optional[str], str, dict]:
        for method, pattern, name, auth in self._ROUTES:
            match = re.fullmatch(pattern, path)
            if method == verb and match:
                return name, auth, match.groupdict()
        return None, "none", {}

    def __call__(self, verb: str, request, body: Optional[bytes]) -> FakeReply:
        url = bytes(request.url().toEncoded()).decode("ascii")
        assert url.startswith(self.base + "/"), url
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        headers = {name: header(request, name) for name in ("Authorization", "Content-Type")}
        self.requests.append((verb, path, body, headers))
        name, auth, params = self.route_of(verb, path)
        if name is None:
            return _error(404, "not_found")

        faults = self.faults.get(name) or []
        fault = faults.pop(0) if faults else None
        if fault == DROP_BEFORE:
            return transport_failure(QNetworkReply.NetworkError.ConnectionRefusedError)
        if isinstance(fault, Fault):
            headers_out = {"Retry-After": str(fault.retry_after)} if fault.retry_after else None
            return _error(fault.status, fault.code, fault.message, detail=fault.detail,
                          headers=headers_out)

        if nip98.has_body(body) and headers["Content-Type"] != b"application/json":
            return json_reply({"ok": False, "error": {"code": "bad_request",
                                                      "message": "Unsupported"}}, status=415)
        who = self._authorize(auth, verb, url, body, headers["Authorization"])
        if isinstance(who, FakeReply):
            return who
        # What a route checks before the body is parsed (its size, an
        # address limit), after the signature as the backend's decorators.
        early = getattr(self, "_before_" + name, None)
        if early is not None:
            refusal = early(body or b"")
            if refusal is not None:
                return refusal
        payload: Any = {}
        if nip98.has_body(body):
            try:
                payload = _strict(body)
            except (ValueError, UnicodeDecodeError):
                return _error(400, "bad_request", "Kein gültiges JSON.")
            if not isinstance(payload, dict):
                return _error(400, "bad_request")
        reply = getattr(self, "_" + name)(who, payload, params, query)
        self.handled.append(name)
        if fault == DROP_AFTER:
            return transport_failure(QNetworkReply.NetworkError.RemoteHostClosedError)
        return reply

    # -- authorization -------------------------------------------------------

    def _refuse(self, reason: str) -> FakeReply:
        self.refusals.append(reason)
        return _error(401, "auth_invalid", "Die Anmeldung ist ungültig. Bitte melde dich neu an.",
                      detail={"reason": {"time window": "expired"}.get(reason, reason)})

    def _authorize(self, auth: str, verb: str, url: str, body: Optional[bytes],
                   value: Optional[bytes]):
        if auth == "none":
            return None
        if not value:
            return _error(401, "auth_required", "Bitte melde dich an.")
        text = value.decode("latin-1")
        if auth in ("session", "session_any"):
            scheme, _, token = text.partition(" ")
            if scheme != "Bearer" or not formats.is_hex64(token):
                return self._refuse("session")
            found = self.sessions.get(hashlib.sha256(bytes.fromhex(token)).hexdigest())
            if found is None:
                return self._refuse("session")
            pubkey, expires = found
            self.last_session = hashlib.sha256(bytes.fromhex(token)).hexdigest()
            if auth == "session" and expires <= self.clock():
                return _error(401, "session_expired",
                              "Die Sitzung ist abgelaufen. Bitte melde dich neu an.")
            return Caller(pubkey=pubkey)
        if text.startswith("Bearer "):
            return _error(401, "auth_invalid", "Diese Aktion braucht eine Signatur.",
                          detail={"reason": "scheme"})
        try:
            event = nip98.check_auth_header(value, url=url, method=verb, body=body,
                                            now=self.clock())
        except nip98.AuthRefused as refusal:
            return self._refuse(refusal.reason)
        if not nip98.has_body(body):
            # Contract 10: a signed action always carries a payload tag.
            return self._refuse("payload")
        if event["id"] in self.seen_ids:
            return self._refuse("replay")
        self.seen_ids.add(event["id"])
        self.accepted.append(event)
        return Caller(pubkey=event["pubkey"], event=event, body=(body or b"").decode("utf-8"))

    # -- views ---------------------------------------------------------------

    def _turn(self, record: Record) -> dict:
        state = record.state
        if state in ("submitted", "in_review"):
            if record.responsible:
                return {"type": "editor", "pubkey": record.responsible, "pending": []}
            return {"type": "redaktion", "pubkey": None, "pending": []}
        if state == "changes_requested":
            return {"type": "writer", "pubkey": record.writer, "pending": []}
        if state == "accepted":
            if record.step == "confirm":
                creators = [record.writer] + record.participants
                pending = [p for p in creators if p not in record.confirmations]
                return {"type": "contributors", "pubkey": None, "pending": pending}
            if record.step == "writer_publishes":
                return {"type": "writer", "pubkey": record.writer, "pending": []}
            return {"type": "releaser", "pubkey": None, "pending": []}
        if state == "published" and not record.placed:
            return {"type": "releaser", "pubkey": None, "pending": []}
        return {"type": "none", "pubkey": None, "pending": []}

    def view(self, record: Record, *, member: bool = False) -> dict:
        """backend core/editorial_views.py submission_view (fde04a3): a
        member's View also carries the internal counter and activity_at;
        a participant's is the same with and without internal notes."""
        current = record.current
        internal = {"last_internal_seq": record.internal_seq, "activity_at": record.activity_at,
                    # Contract-ahead (feat/redaktion 3b00351, 9.3).
                    "last_internal_message_seq": record.last_internal_message_seq}
        return dict(internal if member else {}, **{
            "submission_id": record.sid, "publication": record.publication,
            "kind": record.kind, "title": record.title, "writer": record.writer,
            "participants": list(record.participants), "state": record.state,
            "step": record.step, "turn": self._turn(record), "responsible": record.responsible,
            "marks": list(record.marks), "signer": record.signer, "section": record.section,
            "revision": record.revision, "round": record.round, "p_sub": record.p_sub,
            "epoch": record.epoch, "revision_hash": current["revision_hash"],
            "created_at": record.created_at, "updated_at": record.updated_at,
            "last_seq": record.seq,
            # Contract-ahead (feat/redaktion 3b00351, 9.3): only messages
            # and events move it, never reactions, retractions or edits.
            "last_message_seq": record.last_message_seq,
            "confirmations": {k: list(v) for k, v in record.confirmations.items()},
            "published": record.published, "placed": record.placed, "proposal_by": None,
        })

    def _append(self, record: Record, item: dict) -> int:
        """backend db/editorial_submissions.py _append (fde04a3): an
        internal note moves only the internal counter and activity_at."""
        now = int(self.clock())
        if item["type"] == "internal":
            record.internal_seq += 1
            seq = record.internal_seq
        else:
            record.seq += 1
            record.updated_at = now
            seq = record.seq
        record.activity_at = now
        record.timeline.append(dict(item, seq=seq, at=now))
        return seq

    def _event(self, record: Record, name: str, who: Optional[Caller], **extra) -> int:
        # A revision's signed action is served with the Revision only.
        signed = who.signed if who and name not in REVISION_EVENTS else None
        seq = self._append(record, {"type": "event", "wrap": None, "event": dict(
            {"name": name, "actor": who.pubkey if who else None,
             "revision": None, "to": None, "signed": signed}, **extra)})
        record.last_message_seq = seq
        return seq

    def _role(self, record: Record, pubkey: str) -> Optional[str]:
        if pubkey == record.writer:
            return "writer"
        if pubkey in record.participants:
            return "contributor"
        return self.publications[record.publication].members.get(pubkey)

    def _record(self, sid: str) -> Optional[Record]:
        return self.submissions.get(sid)

    # -- sessions and the instance -----------------------------------------

    def _session(self, who, payload, params, query) -> FakeReply:
        token = secrets.token_hex(32)
        expires = int(self.clock()) + SESSION_SECONDS
        self.sessions[hashlib.sha256(bytes.fromhex(token)).hexdigest()] = (who.pubkey, expires)
        return _ok({"token": token, "pubkey": who.pubkey, "expires_at": expires})

    def _session_delete(self, who, payload, params, query) -> FakeReply:
        self.sessions.pop(self.last_session, None)
        return _ok({})

    def _instance(self, who, payload, params, query) -> FakeReply:
        return _ok({"operator": "Einundzwanzig e.V.", "server": self.base,
                    "web_app_by": "Einundzwanzig e.V.", "bote": self.bote,
                    "inbox_relays": ["wss://inbox.example"]})

    def _nip05(self, who, payload, params, query) -> FakeReply:
        return json_reply({"names": {"bote": self.bote["pubkey"]}})

    def _destinations(self, who, payload, params, query) -> FakeReply:
        items = []
        for pub in self.publications.values():
            if pub.open and not pub.paused:
                content = pub.content
                items.append({"address": pub.address, "name": content.get("name"),
                              "picture": None, "key_event": pub.key_event,
                              "rules": content.get("rules")})
        return _ok({"publications": items})

    def _site(self, params) -> Optional[Publication]:
        for pub in self.publications.values():
            pubkey, site_d = formats.parse_address(pub.address)
            if formats.route_key(pubkey, site_d) == params.get("site"):
                return pub
        return None

    def _settings_get(self, who, payload, params, query) -> FakeReply:
        pub = self._site(params)
        if pub is None:
            return _error(404, "not_found")
        return _ok({"key_event": pub.key_event, "settings_event": None,
                    "members_count": len(pub.members)})

    def _settings_put(self, who, payload, params, query) -> FakeReply:
        pub = self._site(params)
        if pub is None:
            return _error(404, "not_found")
        if pub.members.get(who.pubkey) != "owner":
            return _error(403, "role")
        event = payload.get("key_event")
        if event is not None:
            pubkey, site_d = formats.parse_address(pub.address)
            try:
                content = json.loads(event["content"])
                valid = (_signed(event, 30078) and event["pubkey"] == pubkey
                         and ["d", formats.key_event_d(site_d)] in event["tags"]
                         and content["publication"] == pub.address
                         and content["epoch"] >= pub.epoch)
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                return _error(422, "key_event_invalid")
            pub.key_event = event
        return _ok({"key_event": pub.key_event, "settings_event": payload.get("settings_event")})

    def _member_of(self, params, pubkey) -> Tuple[Optional[Publication], Optional[str]]:
        pub = self._site(params)
        return pub, (pub.members.get(pubkey) if pub else None)

    def _members_get(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None:
            return _error(404, "not_found")
        if role is None:
            return _error(403, "not_member")
        return _ok({"members": [self._member(pub, p) for p in pub.members]})

    def _member(self, pub: Publication, pubkey: str) -> dict:
        role = pub.members[pubkey]
        return {"pubkey": pubkey, "role": role.split(":")[-1],
                "status": "invited" if role.startswith("invited:") else "active",
                "sees": "all", "invited_by": None, "accepted_at": None, "away_until": None,
                "releaser": pubkey in pub.releasers}

    def _members_post(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role != "owner":
            return _error(403, "role")
        if not formats.is_pubkey(payload.get("pubkey")) or \
                payload.get("role") not in ("redaktion", "curator") or \
                (payload.get("sees") == "since_join" and payload.get("history") is not None):
            return _error(422, "invalid")
        pub.members[payload["pubkey"]] = "invited:" + payload["role"]
        return _ok({"member": self._member(pub, payload["pubkey"])})

    def _members_accept(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or not (role or "").startswith("invited:"):
            return _error(403, "not_member")
        pub.members[who.pubkey] = role.split(":", 1)[1]
        return _ok({"member": self._member(pub, who.pubkey)})

    def _members_remove(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role != "owner":
            return _error(403, "role")
        pub.members.pop(payload.get("pubkey"), None)
        return _ok({"epoch": pub.epoch + 1})

    def _members_me(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role is None:
            return _error(403, "not_member")
        return _ok({"member": self._member(pub, who.pubkey)})

    def _epochs(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role != "owner":
            return _error(403, "role")
        return _ok({"epoch": pub.epoch + 1})

    def _keys(self, who, payload, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role is None:
            return _error(403, "not_member")
        content = pub.content
        return _ok({"epochs": [{"epoch": content["epoch"], "pubkey": content["pubkey"],
                                "key_event": pub.key_event, "wrap": None}], "history": None})

    def _keyring_get(self, who, payload, params, query) -> FakeReply:
        ring = self.keyrings.get(who.pubkey, {"version": 0, "pages": []})
        return _ok(dict(ring))

    def _keyring_put(self, who, payload, params, query) -> FakeReply:
        # 12.3: only a member writes a keyring, each page a 32,000-byte
        # plaintext's NIP-44 payload at most.
        if not any(who.pubkey in pub.members for pub in self.publications.values()):
            return _error(403, "not_member")
        pages = payload.get("pages")
        if not isinstance(pages, list) or any(
                not isinstance(p, str) or len(p) > formats.MAX_KEYRING_PAGE_CHARS for p in pages):
            return _error(422, "invalid")
        ring = self.keyrings.get(who.pubkey, {"version": 0, "pages": []})
        if payload.get("expected_version") != ring["version"]:
            return _error(409, "conflict")
        self.keyrings[who.pubkey] = {"version": ring["version"] + 1,
                                        "pages": list(payload.get("pages") or [])}
        return _ok({"version": ring["version"] + 1})

    # -- submissions -------------------------------------------------------

    def _blob_check(self, blobs: List[dict]) -> Tuple[List[str], List[str]]:
        present = [b["sha256"] for b in blobs if b["sha256"] in self.blobs_present]
        missing = [b["sha256"] for b in blobs if b["sha256"] not in self.blobs_present]
        return present, missing

    def _receipt(self, record: Record, revision: int, *, recheck: bool) -> dict:
        stored = record.receipts[revision]
        if recheck:
            present, missing = self._blob_check(record.revisions[revision]["blobs"])
            stored["blobs_present"], stored["blobs_missing"] = present, missing
        stored["state"] = record.state
        receipt = json.loads(json.dumps(stored))
        if self.tamper_receipt is not None:
            receipt = self.tamper_receipt(receipt)
        return receipt

    def _check_revision_parts(self, body: dict, names: set) -> Optional[FakeReply]:
        """A correction body's checks (submit has _parse_submission, from
        the backend; corrections are not yet diffed against it): the wraps
        (exactly one per recipient name, each on the curve, a submission
        key's ``ct`` exactly 348 characters), the files (at most 64, each
        on at most 10 https servers) and the envelope (12.4, 15)."""
        wraps = body.get("wraps")
        if not isinstance(wraps, dict) or set(wraps) != names or not all(
                isinstance(w, dict) and set(w) == {"eph", "ct"} and isinstance(w["ct"], str)
                and len(w["ct"]) == SUBMISSION_WRAP_CT_CHARS and _on_curve(w.get("eph"))
                for w in wraps.values()):
            return _error(422, "wrap_invalid")
        blobs = body.get("blobs")
        if not (isinstance(blobs, list) and len(blobs) <= 64 and all(
                isinstance(b, dict) and _hexes(b, "sha256") and isinstance(b.get("servers"), list)
                and len(b["servers"]) <= 10
                and all(isinstance(u, str) and u.startswith("https://") for u in b["servers"])
                for b in blobs)):
            return _error(422, "invalid", detail={"problem": "blobs"})
        if not _hexes(body, "revision_hash", "envelope_sha256") or not _whole(
                body.get("revision"), 1):
            return _error(422, "invalid")
        try:
            envelope = base64.b64decode(body.get("envelope") or "", validate=True)
        except (ValueError, TypeError):
            return _error(422, "invalid", detail={"problem": "envelope"})
        if len(envelope) > 4 * 1024 * 1024:
            return _error(413, "too_large")
        if hashlib.sha256(envelope).hexdigest() != body["envelope_sha256"]:
            return _error(422, "invalid", detail={"problem": "envelope_sha256"})
        return None

    def _no_decider(self, pub: "Publication", writer: str, participants: List[str]) -> bool:
        """12.4 (5c11475): nobody in the Redaktion outside the writer and
        the contributors could decide."""
        named = {writer, *participants}
        return not any(role in ("owner", "redaktion") and member not in named
                       for member, role in pub.members.items())

    def _work_round(self, address: str, writer: str, coordinate: str) -> Tuple[int, bool]:
        """The round a work is in at a publication and whether it is
        still open (backend core/editorial_intake.py work_round)."""
        earlier = sorted((r for r in self.submissions.values() if r.publication == address
                          and r.writer == writer and r.coordinate_hash == coordinate),
                         key=lambda r: r.round)
        still_open = [r for r in earlier if r.state not in ROUND_CLOSED]
        if still_open:
            return still_open[-1].round, True
        return (earlier[-1].round + 1 if earlier else 1), False

    def _parse_submission(self, data: dict, writer: str) -> dict:
        """backend core/editorial_submission.py parse_submission (fde04a3),
        check for check in its order: the shape and the body's own
        consistency. Written apart from the client's code, keys checked on
        the curve with coincurve. A body by link (``source``) is not
        modelled: MyEditor never sends one."""
        unknown = sorted(set(data) - SUBMIT_FIELDS - ENVELOPE_FIELDS)
        if unknown:
            raise _Refused(422, "invalid", {"fields": unknown})
        missing = sorted((SUBMIT_FIELDS - {"client"} | ENVELOPE_FIELDS) - set(data))
        if missing:
            raise _Refused(422, "invalid", {"fields": missing})
        _need(type(data["v"]) is int and data["v"] == 1, "v")
        _need(_whole(data["round"], 1) and data["round"] <= MAX_SAFE_INTEGER, "round")
        parts = data["publication"].split(":", 2) if isinstance(data["publication"], str) else []
        _need(len(parts) == 3 and parts[0] == "30004" and bool(parts[2]), "publication")
        _need(_on_curve(parts[1]), "publication")
        participants = data["participants"]
        _need(isinstance(participants, list) and len(participants) <= 20, "participants")
        for key in participants:
            _need(_on_curve(key), "participants")
        _need(len(set(participants)) == len(participants) and writer not in participants,
              "participants")
        for name in ("coordinate_hash", "revision_hash", "submission_id"):
            _need(formats.is_hex64(data[name]), name)
        _need(data["submission_id"] == formats.submission_id(
            writer, data["publication"], data["coordinate_hash"], data["round"]),
            "submission_id")
        text = data["envelope"]
        _need(isinstance(text, str), "envelope")
        if len(text) > (MAX_ENVELOPE_BYTES + 2) // 3 * 4:
            raise _Refused(413, "too_large", {"max_bytes": MAX_ENVELOPE_BYTES})
        try:
            raw = base64.b64decode(text, validate=True)
        except (ValueError, TypeError):
            raise _Refused(422, "invalid", {"field": "envelope"}) from None
        _need(len(raw) > ENVELOPE_OVERHEAD, "envelope")
        _need(formats.is_hex64(data["envelope_sha256"]), "envelope_sha256")
        _need(hashlib.sha256(raw).hexdigest() == data["envelope_sha256"], "envelope_sha256")
        _need(_whole(data["revision"], 1) and data["revision"] <= MAX_SAFE_INTEGER, "revision")
        _need(formats.is_hex64(data["key_event_id"]), "key_event_id")
        _need(_whole(data["epoch"], 1) and data["epoch"] <= MAX_SAFE_INTEGER, "epoch")
        _need(data["kind"] in ("article", "audio"), "kind")
        _need(_plain(data["title"], 1, 300), "title")
        _need(data["signer"] in ("writer", "publication"), "signer")
        _need(_on_curve(data["p_sub"]), "p_sub")
        names = {"redaktion", "writer", *participants}
        wraps = data["wraps"]
        _need(isinstance(wraps, dict) and set(wraps) == names, "wraps")
        for name in sorted(names):
            wrap = wraps[name]
            _need(isinstance(wrap, dict) and set(wrap) == {"eph", "ct"}
                  and _on_curve(wrap["eph"]) and isinstance(wrap["ct"], str)
                  and len(wrap["ct"]) == SUBMISSION_WRAP_CT_CHARS and _nip44_shaped(wrap["ct"]),
                  f"wraps.{name}", code="wrap_invalid")
        _need(formats.is_hex64(data["status_key_hash"]), "status_key_hash")
        _need(_plain(data.get("client", ""), 0, 64), "client")
        blobs = data["blobs"]
        _need(isinstance(blobs, list) and len(blobs) <= 64, "blobs")
        for blob in blobs:
            _need(isinstance(blob, dict), "blobs")
            unknown = sorted(set(blob) - {"sha256", "size", "servers"})
            missing = sorted({"sha256", "size", "servers"} - set(blob))
            if unknown or missing:
                raise _Refused(422, "invalid", {"fields": unknown or missing})
            servers = blob["servers"]
            _need(isinstance(servers, list) and 1 <= len(servers) <= 10
                  and all(isinstance(u, str) and u.startswith("https://") and len(u) <= 512
                          for u in servers) and len(set(servers)) == len(servers),
                  "blobs.servers")
            _need(formats.is_hex64(blob["sha256"]), "blobs.sha256")
            _need(_whole(blob["size"], 1) and blob["size"] <= MAX_SAFE_INTEGER, "blobs.size")
        _need(len({b["sha256"] for b in blobs}) == len(blobs), "blobs")
        return dict(data, envelope_size=len(raw))

    def _check_destination(self, pub: "Publication", body: dict, writers: set) -> None:
        """backend core/editorial_intake.py check_destination (fde04a3)."""
        rules = pub.content["rules"]
        if writers & pub.blocked:
            raise _Refused(403, "blocked")
        if (body["epoch"], body["key_event_id"]) != (pub.epoch, pub.key_event["id"]):
            raise _Refused(409, "epoch", {"epoch": pub.epoch, "key_event_id": pub.key_event["id"]})
        if body["kind"] not in rules["kinds"]:
            raise _Refused(403, "forbidden", {"field": "kind"})
        if rules["publish_by"][body["kind"]] not in (body["signer"], "both"):
            raise _Refused(403, "forbidden", {"field": "signer"})
        if body["envelope_size"] > rules["max_envelope_bytes"]:
            raise _Refused(413, "too_large", {"max_bytes": rules["max_envelope_bytes"]})

    def _check_new(self, pub: "Publication", body: dict, writer: str) -> None:
        """backend core/editorial_intake.py _check_new (fde04a3)."""
        if pub.paused or not pub.open:
            raise _Refused(403, "paused", message="Die Publikation nimmt gerade keine "
                                                  "Einreichungen an.")
        if body["revision"] != 1:
            raise _Refused(409, "stale_revision", {"revision": 0})
        round_, still_open = self._work_round(pub.address, writer, body["coordinate_hash"])
        if still_open or body["round"] != round_:
            # "open" is contract-ahead (feat/redaktion 3b00351, section 2).
            raise _Refused(409, "conflict", {"round": round_ if still_open else round_ - 1,
                                             "open": still_open},
                           message="Dieses Werk liegt hier schon vor.")
        limit = pub.content["rules"].get("max_open_per_writer", 3)
        if len([r for r in self.submissions.values() if r.writer == writer
                and r.publication == pub.address and r.state in OPEN]) >= limit:
            raise _Refused(403, "limit_open", {"limit": limit})

    @staticmethod
    def _refuse_over_cap(record: Record) -> None:
        """backend core/editorial_quota.py refuse_revision_over_cap."""
        if record.revision >= MAX_REVISIONS:
            raise _Refused(403, "forbidden", {"limit": MAX_REVISIONS})

    def _before_submit(self, raw: bytes) -> Optional[FakeReply]:
        if len(raw) > SUBMIT_BODY_BYTES:
            return _error(413, "too_large", "Die Anfrage ist zu groß.",
                          detail={"max_bytes": SUBMIT_BODY_BYTES})
        return None

    def _submit(self, who, body, params, query) -> FakeReply:
        try:
            return self._hand_in(who, body)
        except _Refused as refusal:
            return refusal.reply

    def _hand_in(self, who, data: dict) -> FakeReply:
        """backend api/submissions.py submit and core/editorial_intake.py
        (fde04a3), in their order: the body, the request limits (refused
        requests count), the publication, a repeat before anything else,
        the refusals that read only, then storing (whose limits count
        only what is stored)."""
        writer = who.pubkey
        body = self._parse_submission(data, writer)
        for scope, subject, limit in (("address:submit", "fake", SUBMIT_REQUESTS_PER_ADDRESS_PER_HOUR),
                                      ("submit_request", writer, SUBMIT_REQUESTS_PER_HOUR)):
            wait = self._hit(scope, subject, limit=limit, window=3600)
            if wait:
                raise _Refused(429, "rate_limited", retry_after=wait,
                               message="Zu viele Einreichungen. Bitte später noch einmal.")
        pub = self.publications.get(body["publication"])
        if pub is None:
            raise _Refused(404, "not_found",
                           message="Diese Publikation nimmt hier keine Einreichungen an.")
        record = self._record(body["submission_id"])
        stored = record.revisions.get(body["revision"]) if record is not None else None
        if stored is not None and stored["revision_hash"] == body["revision_hash"]:
            # The same revision again (12.4): the stored receipt, the files
            # it reported missing looked for again.
            return _ok({"receipt": self._receipt(record, body["revision"], recheck=True)})
        owner = record.writer if record is not None else writer
        self._check_destination(pub, body, {writer, owner})
        if self._no_decider(pub, writer, body["participants"]):
            raise _Refused(409, "no_decider")
        if record is None:
            self._check_new(pub, body, writer)
        else:
            self._refuse_over_cap(record)
        if any(r.status_key_hash == body["status_key_hash"] and r.sid != body["submission_id"]
               for r in self.submissions.values()):
            raise _Refused(409, "conflict", {"field": "status_key_hash"})
        now = int(self.clock())
        if record is not None:
            if body["revision"] != record.revision + 1 or \
                    body["revision_hash"] == record.current["revision_hash"]:
                raise _Refused(409, "stale_revision", {"revision": record.revision})
            if (body["kind"], body["p_sub"]) != (record.kind, record.p_sub):
                raise _Refused(409, "conflict")
            new_state = transition(record.state, "revise", "writer")
            if new_state is None:
                raise _Refused(409, "invalid_transition",
                               {"action": "revise", "state": record.state})
        counted = dict(self._counters)          # the transaction: kept only when stored
        try:
            wait = self._hit("submit", writer, limit=REVISIONS_PER_HOUR, window=3600) or \
                self._hit("stored_bytes", f"{owner}:{pub.address}", limit=STORED_BYTES_PER_DAY,
                          window=86400, cost=len(body["envelope"]) + sum(
                              len(formats.canonical_bytes(w)) for w in body["wraps"].values()))
            if wait:
                raise _Refused(429, "rate_limited", retry_after=wait)
        except _Refused:
            self._counters = counted
            raise
        if record is None:
            record = Record(
                sid=body["submission_id"], writer=writer, publication=pub.address,
                title=body["title"], kind=body["kind"], round=body["round"],
                signer=body["signer"], participants=list(body["participants"]),
                p_sub=body["p_sub"], epoch=body["epoch"],
                status_key_hash=body["status_key_hash"],
                coordinate_hash=body["coordinate_hash"], created_at=now)
            self.submissions[record.sid] = record
            event_name = "submitted"
        else:
            if record.state != "changes_requested" and "new_revision" not in record.marks:
                record.marks.append("new_revision")
            record.state = new_state
            record.title, record.signer = body["title"], body["signer"]
            record.status_key_hash, record.epoch = body["status_key_hash"], body["epoch"]
            event_name = "revision"
        record.participants = list(body["participants"])     # set_participants
        record.revisions[body["revision"]] = {
            "revision": body["revision"], "revision_hash": body["revision_hash"],
            "envelope": body["envelope"], "envelope_sha256": body["envelope_sha256"],
            "wraps": body["wraps"], "blobs": body["blobs"], "created_at": now,
            "signed": who.signed}
        seq = self._event(record, event_name, who, revision=body["revision"])
        present, missing = self._blob_check(body["blobs"])
        record.receipts[body["revision"]] = {
            "v": 1, "submission_id": record.sid, "revision": body["revision"],
            "revision_hash": body["revision_hash"],
            "envelope_sha256": body["envelope_sha256"],
            "wraps_sha256": {n: formats.wrap_sha256(w) for n, w in body["wraps"].items()},
            "blobs_present": present, "blobs_missing": missing, "received_at": now,
            "state": record.state, "seq": seq}
        return _ok({"receipt": self._receipt(record, body["revision"], recheck=False)},
                   status=201)

    def _before_lookup(self, raw: bytes) -> Optional[FakeReply]:
        """backend api/submission_reads.py lookup (fde04a3): every request
        counts against the address, then the body's size, before parsing."""
        wait = self._hit("address:lookup", "fake", limit=LOOKUPS_PER_ADDRESS_PER_MINUTE,
                         window=60)
        if wait:
            return _error(429, "rate_limited", "Zu viele Abfragen. Bitte gleich noch einmal.",
                          headers={"Retry-After": str(wait)})
        if len(raw) > LOOKUP_BODY_BYTES:
            return _error(413, "too_large", detail={"max_bytes": LOOKUP_BODY_BYTES})
        return None

    def _lookup(self, who, body, params, query) -> FakeReply:
        """backend api/submission_reads.py _lookup_items and lookup
        (fde04a3): the items checked in order, the rows answered in the
        order asked, one timeline budget shared by all of them."""
        refusal = _fields(body, {"items"}, {"items"})
        if refusal is not None:
            return refusal
        items = body["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= formats.MAX_LOOKUP_ITEMS:
            return _error(422, "invalid", detail={"field": "items"})
        wanted: Dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                return _error(422, "invalid", detail={"field": "items"})
            refusal = _fields(item, {"status_key", "since"}, {"status_key", "since"})
            if refusal is not None:
                return refusal
            if not formats.is_hex64(item["status_key"]):
                return _error(422, "invalid", detail={"field": "status_key"})
            key_hash = formats.status_key_hash(item["status_key"])
            if key_hash in wanted:
                return _error(422, "invalid", detail={"field": "items"})
            if type(item["since"]) is not int or not 0 <= item["since"] <= MAX_SAFE_INTEGER:
                return _error(422, "invalid", detail={"field": "since"})
            wanted[key_hash] = item["since"]
        by_hash = {r.status_key_hash: r for r in self.submissions.values()}
        budget = TimelineBudget(self.timeline_bytes)
        limit = self.timeline_per_answer["lookup"]
        answers = []
        for key_hash, since in wanted.items():
            record = by_hash.get(key_hash)
            if record is None:
                continue
            timeline, more = budget.page(self._timeline(record, since, limit=limit + 1),
                                         limit=limit)
            answers.append({
                "status_key_hash": key_hash, "submission": self.view(record),
                "receipt": self._receipt(record, record.revision, recheck=False),
                "timeline": timeline, "more": more})
        return _ok({"items": answers})

    def _timeline(self, record: Record, since: int, *, limit: int, member: bool = False,
                  since_internal: int = 0) -> List[dict]:
        """backend db/editorial_submissions.py timeline (fde04a3): the
        conversation after ``since``, and for a member the internal notes
        after ``since_internal``, each channel in its own order, merged by
        time, at most ``limit``."""
        conversation = [dict(i) for i in record.timeline
                        if i["type"] != "internal" and i["seq"] > since][:limit]
        if not member:
            return conversation
        internal = [dict(i) for i in record.timeline
                    if i["type"] == "internal" and i["seq"] > since_internal][:limit]
        return list(heapq.merge(conversation, internal, key=lambda i: i["at"]))[:limit]

    def _is_member(self, record: Record, pubkey: str) -> bool:
        """backend api/editorial_context.py is_member: whatever the primary
        role, a member reads internal notes (a writer who is one too)."""
        return self.publications[record.publication].members.get(pubkey) in (
            "owner", "redaktion", "curator")

    def _submission_get(self, who, body, params, query) -> FakeReply:
        record = self._record(params["sid"])
        if record is None:
            return _error(404, "not_found")
        role = self._role(record, who.pubkey)
        if role is None or role.startswith("invited:"):
            # backend submission_access: nobody without a role learns it exists.
            return _error(404, "not_found")
        numbers = {}
        for name, default, high in (("since", 0, MAX_SAFE_INTEGER),
                                    ("since_internal", 0, MAX_SAFE_INTEGER),
                                    ("revision", record.revision, record.revision)):
            raw = (query.get(name) or [None])[0]
            if raw is not None and not (re.fullmatch(r"[0-9]{1,16}", raw)
                                        and (name != "revision" or int(raw) >= 1)
                                        and int(raw) <= high):
                return _error(422, "invalid", detail={"field": name})
            numbers[name] = default if raw is None else int(raw)
        member = self._is_member(record, who.pubkey)
        # Contract 12.4 (5c11475): the latest revision, or only ?revision=n.
        revision = dict(record.revisions[numbers["revision"]])
        # backend wrap_names: the writer's own wrap first, then a contributor's.
        mine = {"writer": "writer", "contributor": who.pubkey}.get(role, "redaktion")
        revision["wraps"] = {k: v for k, v in revision["wraps"].items() if k == mine}
        pub = self.publications[record.publication]
        limit = self.timeline_per_answer["get"]
        timeline, more = TimelineBudget(self.timeline_bytes).page(
            self._timeline(record, numbers["since"], limit=limit + 1, member=member,
                           since_internal=numbers["since_internal"]), limit=limit)
        return _ok({"submission": self.view(record, member=member), "my_role": role,
                    "revisions": [revision], "timeline": timeline, "more": more,
                    "members": [self._member(pub, p) for p in pub.members]})

    def _messages(self, who, body, params, query) -> FakeReply:
        record = self._record(params["sid"])
        if record is None:
            return _error(404, "not_found")
        role = self._role(record, who.pubkey)
        if role is None:
            return _error(403, "forbidden")
        wrap = body.get("wrap")
        if body.get("kind_hint") not in ("message", "reaction", "retraction", "edit") \
                or not _signed(wrap, 1059) \
                or len(formats.canonical_bytes(wrap)) > formats.MAX_WRAP_EVENT_BYTES:
            return _error(422, "invalid")
        channel = body.get("channel")
        if channel == "internal" and not self._is_member(record, who.pubkey):
            return _error(403, "role")
        if channel not in ("conversation", "internal"):
            return _error(400, "bad_request")
        action = {"internal": "internal_note"}.get(channel) or (
            "message" if body["kind_hint"] in ("message", "edit") else "react")
        new_state = transition(record.state, action, role)
        if new_state is not None:
            record.state = new_state
        seq = self._append(record, {"type": "message" if channel == "conversation"
                                    else "internal", "wrap": wrap, "event": None})
        if channel == "conversation" and body["kind_hint"] == "message":
            record.last_message_seq = seq
        elif body["kind_hint"] == "message":
            record.last_internal_message_seq = seq
        return _ok({"seq": seq})

    def _signed_record(self, who, body, params) -> Tuple[Optional[Record], Optional[FakeReply], str]:
        record = self._record(params["sid"])
        if record is None:
            return None, _error(404, "not_found"), ""
        if body.get("submission_id") != record.sid:
            return None, _error(422, "invalid", detail={"problem": "submission_id"}), ""
        role = self._role(record, who.pubkey) or ""
        return record, None, role

    def _decision(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        if role not in REDAKTION:
            return _error(403, "role")
        if body.get("expected_revision") != record.revision:
            return _error(409, "stale_revision")
        outcome = body.get("outcome")
        action = outcome
        if outcome == "request_changes" and not body.get("message_wrap"):
            action = "request_changes_without_message"
        new_state = transition(record.state, action, role)
        if new_state is None:
            if action == "request_changes_without_message":
                return _error(422, "invalid", "Änderungen brauchen eine Nachricht.")
            return _error(409, "invalid_transition")
        record.state = new_state
        if new_state == "accepted":
            record.step = "writer_publishes" if record.signer == "writer" else "confirm"
        if body.get("message_wrap"):
            self._append(record, {"type": "message", "wrap": body["message_wrap"],
                                  "event": None})
        seq = self._event(record, outcome, who)
        return _ok({"submission": self.view(record), "seq": seq})

    def _confirmation(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        if role not in ("writer", "contributor"):
            return _error(403, "role")
        if body.get("revision_hash") != record.current["revision_hash"]:
            return _error(409, "stale_revision")
        confirms = body.get("confirms") or []
        known = record.confirmations.setdefault(who.pubkey, [])
        known.extend(c for c in confirms if c not in known)
        creators = [record.writer] + record.participants
        if record.state == "accepted" and record.step == "confirm" and \
                all(p in record.confirmations for p in creators):
            record.step = "ready_for_release"
        self._event(record, "confirmed", who)
        return _ok({"submission": self.view(record)})

    def _responsible(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        action = body.get("action")
        if action not in ("take", "hand_over", "hand_back"):
            return _error(422, "invalid")
        new_state = transition(record.state, "take", role)
        if new_state is None:
            return _error(403 if role not in REDAKTION else 409,
                          "role" if role not in REDAKTION else "invalid_transition")
        record.state = new_state
        record.responsible = {"take": who.pubkey, "hand_over": body.get("to"),
                              "hand_back": None}[action]
        self._event(record, {"take": "taken", "hand_over": "handed_over",
                             "hand_back": "handed_back"}[action], who)
        return _ok({"submission": self.view(record)})

    def _withdraw(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        if role != "writer":
            return _error(403, "role")
        new_state = transition(record.state, "withdraw", role)
        if new_state is None:
            return _error(409, "invalid_transition")
        record.state = new_state
        self._event(record, "withdrawn", who)
        return _ok({"submission": self.view(record)})

    def _published(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        event = body.get("event")
        if not _signed(event, 30023):
            return _error(422, "invalid")
        pub = self.publications[record.publication]
        if record.signer == "writer":
            d = formats.tag_value(event, "d") or ""
            if role != "writer" or event["pubkey"] != record.writer or \
                    formats.coordinate_hash(event["pubkey"], d) != record.coordinate_hash:
                return _error(422, "invalid")
            action = "published_by_writer"
        else:
            role = "releaser" if who.pubkey in pub.releasers else role
            action = "published_by_release"
        new_state = transition(record.state, action, role)
        if new_state is None:
            return _error(409, "invalid_transition")
        record.state = new_state
        record.step = None
        record.published = {"naddr": body.get("naddr"), "event_id": event["id"]}
        self._event(record, "published", who)
        return _ok({"submission": self.view(record)})

    def _placed(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        pub = self.publications[record.publication]
        role = "releaser" if who.pubkey in pub.releasers else role
        if transition(record.state, "placed", role) is None:
            return _error(409 if role == "releaser" else 403,
                          "invalid_transition" if role == "releaser" else "role")
        record.placed = True
        self._event(record, "placed", who)
        return _ok({"submission": self.view(record)})

    def _corrections(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        if role not in REDAKTION:
            return _error(403, "role")
        refusal = self._check_revision_parts(
            body, {"redaktion", "writer", *record.participants})
        if refusal is not None:
            return refusal
        if body.get("revision") != record.revision + 1:
            return _error(409, "stale_revision")
        if body["revision"] > MAX_REVISIONS:
            return _error(422, "invalid", detail={"problem": "revisions"})
        record.revisions[body["revision"]] = {k: body.get(k) for k in (
            "revision", "revision_hash", "envelope", "envelope_sha256", "wraps", "blobs")}
        record.revisions[body["revision"]].update(
            created_at=int(self.clock()), signed=who.signed)
        if "changed_since_acceptance" not in record.marks:
            record.marks.append("changed_since_acceptance")
        seq = self._event(record, "correction", who, revision=body["revision"])
        return _ok({"submission": self.view(record), "seq": seq})

    def _remind(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        if role != "writer" or body.get("to") not in record.participants:
            return _error(403, "role")
        return _ok({})

    def _reupload(self, who, body, params, query) -> FakeReply:
        record, refusal, role = self._signed_record(who, body, params)
        if refusal is not None:
            return refusal
        if role not in ("owner", "redaktion", "curator"):
            return _error(403, "role")
        return _ok({})

    def _proposals(self, who, body, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role not in ("owner", "redaktion", "curator"):
            return _error(403, "role")
        return _error(422, "invalid", "Vorschläge kann diese Testumgebung nicht.")

    def _site_submissions(self, who, body, params, query) -> FakeReply:
        pub, role = self._member_of(params, who.pubkey)
        if pub is None or role is None:
            return _error(403, "not_member")
        return _ok({"items": [self.view(r) for r in self.submissions.values()
                              if r.publication == pub.address], "next": None})

    def _me_editorial(self, who, body, params, query) -> FakeReply:
        me = who.pubkey
        mine = [self.view(r) for r in self.submissions.values()
                if self._turn(r).get("pubkey") == me]
        invited = [p for p in self.publications.values()
                   if p.members.get(me, "").startswith("invited:")]
        return _ok({"turns": mine, "counts": {"mine": len(mine)},
                    "publications": [{"address": p.address, "role": p.members[me],
                                      "name": p.content.get("name")}
                                     for p in self.publications.values()
                                     if me in p.members and p not in invited],
                    "invitations": [{"address": p.address, "name": p.content.get("name"),
                                     "role": p.members[me].split(":", 1)[1], "sees": "all",
                                     "invited_by": None, "invited_at": None} for p in invited]})

    def _me_submissions(self, who, body, params, query) -> FakeReply:
        return _ok({"items": [self.view(r) for r in self.submissions.values()
                              if who.pubkey == r.writer or who.pubkey in r.participants]})


# =========================================================================== #
# Signing as a person, and a submission to send                               #
# =========================================================================== #

class SignAs:
    """A ``SignFn`` (``sign(unsigned, on_success, on_failure)``) signing
    with ``secret``. ``takes`` advances ``clock`` that many seconds per
    signature (a list: one per signature), the way a person looking for
    their phone does; ``failure`` refuses every request."""

    def __init__(self, secret: bytes, *, clock=None, takes=0.0,
                 failure: Optional[str] = None) -> None:
        self.secret = bytes(secret)
        self.pubkey = crypto.get_public_key(self.secret).hex()
        self.clock = clock
        self.takes = list(takes) if isinstance(takes, (list, tuple)) else takes
        self.failure = failure
        self.requests: List[dict] = []

    def __call__(self, unsigned: dict, on_success, on_failure) -> None:
        self.requests.append(json.loads(json.dumps(unsigned)))
        if self.failure is not None:
            on_failure(self.failure)
            return
        duration = self.takes.pop(0) if isinstance(self.takes, list) else self.takes
        if self.clock is not None and duration:
            self.clock.advance(duration)
        on_success(events.sign_event(dict(unsigned), self.secret))


@dataclass
class Prepared:
    """A submission ready to send, and the secrets the writer keeps."""

    body: dict
    s_sub: bytes
    status_key: str
    revision: dict


def submission_for(writer_sk: bytes, *, key_event: Optional[dict] = None, revision: int = 1,
                   d: str = "ein-jahr-mit-lightning", round_: int = 1,
                   title: str = "Ein Jahr mit Lightning im Café",
                   content: str = "# Ein Jahr\n\nText.", blobs: Tuple[dict, ...] = (),
                   participants: Tuple[str, ...] = (), s_sub: Optional[bytes] = None,
                   status_key: Optional[str] = None) -> Prepared:
    """A complete, valid ``POST /api/submissions`` body for the fixture's
    publication, built with the real code (M5): revision, envelope,
    wraps to the epoch key and to the writer."""

    event = key_event or KEY_EVENT
    content_json = json.loads(event["content"])
    writer = crypto.get_public_key(writer_sk).hex()
    address = content_json["publication"]
    coordinate = formats.coordinate_hash(writer, d)
    sid = formats.submission_id(writer, address, coordinate, round_)
    s_sub = s_sub or scrypto.new_secret_key()
    status_key = status_key or scrypto.new_status_key()
    rev = formats.build_revision(
        submission_id=sid, revision=revision, kind="article",
        work={"title": title, "content": content, "d": d},
        rights={"confirmed": True, "license": None}, client="MyEditor tests")
    sealed = scrypto.seal_revision(rev, s_sub)
    recipients = {"redaktion": content_json["pubkey"], "writer": writer}
    recipients.update({p: p for p in participants})
    wraps = scrypto.wrap_submission_key(sid, s_sub, recipients)
    # Assembled by hand from contract 12.4, apart from the client's
    # submission_body, so each checks the other.
    body = {
        "v": 1, "submission_id": sid, "round": round_, "revision": revision,
        "revision_hash": sealed.revision_hash, "publication": address,
        "key_event_id": event["id"], "epoch": content_json["epoch"], "kind": "article",
        "title": title, "coordinate_hash": coordinate, "signer": "writer",
        "participants": list(participants), "p_sub": scrypto.pubkey_of(s_sub),
        "envelope": sealed.envelope_b64, "envelope_sha256": sealed.envelope_sha256,
        "wraps": wraps, "blobs": [dict(b) for b in blobs],
        "status_key_hash": hashlib.sha256(bytes.fromhex(status_key)).hexdigest(),
        "client": "MyEditor tests"}
    return Prepared(body=body, s_sub=s_sub, status_key=status_key, revision=rev)


# =========================================================================== #
# A signer app on the other end of NIP-46                                     #
# =========================================================================== #

class Nip46SignerApp:
    """A remote signer that answers ``sign_event``, ``nip44_encrypt`` and
    ``nip44_decrypt`` with real keys over the handshake tests' FakePool,
    the way a signer app does: every request and every answer is one
    NIP-44 message, so an answer that would be larger than 65,535 bytes
    is never sent (nostr-tools throws there), and the app waits for
    nothing. ``too_large`` counts those; ``largest_plaintext`` is the
    longest text it was asked to encrypt or decrypt, which the 40,960
    bytes of ``signer_limits.SIGNER_NIP44_MAX_PLAINTEXT`` keep within one
    answer. Use :func:`paired_bunker`."""

    def __init__(self, pool, user_sk: bytes) -> None:
        self.pool = pool
        self.sk = crypto.generate_secret_key()
        self.pk = crypto.get_public_key(self.sk).hex()
        self.user_sk = bytes(user_sk)
        self.user_pk = crypto.get_public_key(self.user_sk).hex()
        self.seen: List[dict] = []
        self.too_large = 0
        self.largest_request = 0
        self.largest_plaintext = 0

    def watch(self, client_pk: str) -> None:
        self.client_pk = client_pk
        self.conv = crypto.conversation_key(self.sk, bytes.fromhex(client_pk))
        self._sub = self.pool.subscribe([], [{"kinds": [24133], "#p": [self.pk]}])
        self._sub.event.connect(self._on_event)

    def _on_event(self, event: dict) -> None:
        if event.get("pubkey") == self.pk:
            return
        try:
            text = crypto.decrypt(event["content"], self.conv)
            payload = json.loads(text)
        except (ValueError, KeyError):
            return
        if "method" not in payload:
            return
        self.largest_request = max(self.largest_request, len(text.encode("utf-8")))
        self.seen.append({"method": payload["method"]})
        rid, method, params = payload.get("id"), payload["method"], payload.get("params") or []
        try:
            if method == "connect":
                result = "ack"
            elif method == "get_public_key":
                result = self.user_pk
            elif method == "ping":
                result = "pong"
            elif method == "sign_event":
                result = json.dumps(events.sign_event(json.loads(params[0]), self.user_sk))
            elif method == "nip44_encrypt":
                self.largest_plaintext = max(self.largest_plaintext,
                                             len(params[1].encode("utf-8")))
                result = crypto.encrypt_to(params[1], self.user_sk, bytes.fromhex(params[0]))
            elif method == "nip44_decrypt":
                result = crypto.decrypt_from(params[1], self.user_sk, bytes.fromhex(params[0]))
                self.largest_plaintext = max(self.largest_plaintext,
                                             len(result.encode("utf-8")))
            else:
                self._send({"id": rid, "error": "unknown method"})
                return
        except ValueError as exc:
            self._send({"id": rid, "error": str(exc)})
            return
        self._send({"id": rid, "result": result})

    def _send(self, payload: dict) -> None:
        try:
            content = crypto.encrypt(json.dumps(payload), self.conv)
        except ValueError:
            self.too_large += 1
            return
        self.pool.publish([], events.build_event(kind=24133, content=content,
                                                 tags=[["p", self.client_pk]], sk=self.sk))


def paired_bunker(user_sk: bytes):
    """A real ``BunkerClient`` paired with a :class:`Nip46SignerApp` for
    ``user_sk``: ``(client, signer_app)``."""
    from nostr import bunker
    from tests.test_bunker_handshake import FakePool

    pool = FakePool()
    app = Nip46SignerApp(pool, user_sk)
    client = bunker.BunkerClient(pool)
    local_sk = crypto.generate_secret_key()
    app.watch(crypto.get_public_key(local_sk).hex())
    outcome: Dict[str, str] = {}
    client.connect_to_bunker(
        f"bunker://{app.pk}?relay=wss%3A%2F%2Ffake&secret=abc",
        on_success=lambda pk: outcome.setdefault("ok", pk),
        on_failure=lambda r: outcome.setdefault("err", r),
        local_sk=local_sk)
    assert outcome.get("ok") == app.user_pk, outcome
    return client, app
