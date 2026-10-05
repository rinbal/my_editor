# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fakes for the EINUNDZWANZIG membership API tests.

A fake transport (``FakeNam`` / ``FakeReply``) that settles only when a
test says so, a signer that really signs (with a throwaway key) so the
server side can verify, a clock, a timer, and :class:`FakeMembershipServer`,
which refuses a body that is not JSON and then applies the spec's NIP-98
checks through the same :func:`nostr.nip98.check_auth_header` the
membership sidecar uses (kind, method, the exact URL, the 60 second
window, the payload hash, the lowercase hex, the signature), plus the
one-time id. A client change that only satisfies our own reader is not a
working client, so the tests get a counterparty that checks.

``FakeReply.rawHeader`` accepts a ``str`` name only, because that is all
PySide6 6.11 accepts. A ``bytes`` lookup silently lost the Retry-After
header once; the fake now fails the same way the real binding does.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QByteArray, QObject, Signal
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

from nostr import crypto, events, nip98


SECRET_KEY = bytes.fromhex("3f" * 32)
PUBKEY = crypto.get_public_key(SECRET_KEY).hex()
BASE = "https://verein.einundzwanzig.space"
PREFIX = BASE + "/api/v1/membership"
# The membership service (sidecar/) the app sends requests to. It adds the
# association's key; the app never has it.
SERVICE = "https://e21.sidecar.example"
SERVICE_PREFIX = SERVICE + "/api/v1/membership"
NOW = 1_785_062_400


# --------------------------------------------------------------------------- #
# Time                                                                         #
# --------------------------------------------------------------------------- #

class FakeClock:
    """Unix seconds that only move when a test moves them."""

    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTimer(QObject):
    """A single-shot timer a test fires by hand."""

    timeout = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.active = False
        self.single_shot = False
        self.started: List[Optional[int]] = []

    def setSingleShot(self, value: bool) -> None:
        self.single_shot = bool(value)

    def start(self, ms: Optional[int] = None) -> None:
        self.active = True
        self.started.append(ms)

    def stop(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active

    def fire(self) -> None:
        assert self.active, "timer fired while not running"
        self.active = False
        self.timeout.emit()


# --------------------------------------------------------------------------- #
# Transport                                                                    #
# --------------------------------------------------------------------------- #

class FakeReply(QObject):
    """Reply stand-in. Nothing happens until a test calls ``finish``."""

    finished = Signal()
    downloadProgress = Signal(int, int)

    def __init__(self, *, status: int = 200, body: bytes = b"",
                 error=QNetworkReply.NetworkError.NoError,
                 headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self._status = status
        self._body = body
        self._error = error
        self._headers = {k.lower(): v.encode("latin-1") for k, v in (headers or {}).items()}
        self.aborted = False
        self.settled = False

    def error(self):
        return self._error

    def attribute(self, attr):
        if attr == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
            return self._status or None
        return None

    def rawHeader(self, name):
        if not isinstance(name, str):
            raise TypeError("rawHeader takes a str header name")
        return QByteArray(self._headers.get(name.lower(), b""))

    def readAll(self) -> QByteArray:
        return QByteArray(self._body)

    def abort(self) -> None:
        self.aborted = True
        self._error = QNetworkReply.NetworkError.OperationCanceledError
        self._status = 0

    def deleteLater(self) -> None:
        pass

    def finish(self) -> None:
        self.settled = True
        self.finished.emit()


def json_reply(payload: Any, *, status: int = 200,
               headers: Optional[Dict[str, str]] = None) -> FakeReply:
    return FakeReply(status=status, body=json.dumps(payload).encode("utf-8"),
                     headers=headers)


def data_reply(data: Any, *, status: int = 200) -> FakeReply:
    return json_reply({"data": data}, status=status)


def transport_failure(error) -> FakeReply:
    return FakeReply(status=0, error=error)


class FakeNam(QObject):
    """Records every request and hands out replies.

    Replies come from ``script`` in order, then from ``responder`` (a
    callable taking ``verb, request, body``), then a bare 200.
    """

    def __init__(self, script=None, *, responder=None) -> None:
        super().__init__()
        self.calls: List[tuple] = []     # (verb, QNetworkRequest, body or None)
        self.issued: List[FakeReply] = []
        self._script = list(script or [])
        self._responder = responder

    def _issue(self, verb: str, request, body: Optional[bytes] = None) -> FakeReply:
        self.calls.append((verb, request, body))
        if self._script:
            reply = self._script.pop(0)
        elif self._responder is not None:
            reply = self._responder(verb, request, body)
        else:
            reply = FakeReply()
        self.issued.append(reply)
        return reply

    def get(self, request):
        return self._issue("GET", request)

    def post(self, request, data):
        return self._issue("POST", request, bytes(data))

    def deleteResource(self, request):
        return self._issue("DELETE", request)

    def settle(self, *, limit: int = 50) -> None:
        """Finish every outstanding reply, including ones that finishing
        another one caused."""
        for _ in range(limit):
            pending = [r for r in self.issued if not r.settled]
            if not pending:
                return
            pending[0].finish()
        raise AssertionError("fake transport did not settle")


def header(request, name: str) -> Optional[bytes]:
    """A request header's value, or None when it was not set."""
    if not request.hasRawHeader(name):
        return None
    return bytes(request.rawHeader(name))


def decode_authorization(value: bytes) -> dict:
    """``Nostr <standard base64 of the event JSON>``, strictly."""
    text = bytes(value).decode("ascii")
    if not text.startswith("Nostr "):
        raise ValueError("authorization scheme is not Nostr")
    raw = base64.b64decode(text[len("Nostr "):], validate=True)
    return json.loads(raw.decode("utf-8"))


def tag(event: dict, name: str) -> Optional[str]:
    for entry in event.get("tags") or []:
        if isinstance(entry, list) and len(entry) >= 2 and entry[0] == name:
            return entry[1]
    return None


# --------------------------------------------------------------------------- #
# Signing                                                                      #
# --------------------------------------------------------------------------- #

class FakeSigner:
    """A signer that really signs, with a throwaway key.

    ``defer`` holds every request until :meth:`release`. ``failure``
    refuses every request with that reason. ``takes`` advances ``clock``
    by that many seconds per signature, the way a person hunting for
    their phone does; a list gives one duration per signature, in
    order. ``tamper`` edits the signed event before it is handed back.
    """

    def __init__(self, *, clock: Optional[FakeClock] = None, defer: bool = False,
                 failure: Optional[str] = None, takes=0.0,
                 tamper: Optional[Callable[[dict], dict]] = None) -> None:
        self.clock = clock
        self.defer = defer
        self.failure = failure
        self.takes = list(takes) if isinstance(takes, (list, tuple)) else takes
        self.tamper = tamper
        self.requests: List[dict] = []
        self.pending: List[tuple] = []

    def __call__(self, unsigned: dict, on_success, on_failure) -> None:
        self.requests.append(json.loads(json.dumps(unsigned)))
        if self.failure is not None:
            on_failure(self.failure)
            return
        if self.defer:
            self.pending.append((unsigned, on_success, on_failure))
            return
        self._answer(unsigned, on_success)

    def _answer(self, unsigned: dict, on_success) -> None:
        duration = self.takes.pop(0) if isinstance(self.takes, list) else self.takes
        if self.clock is not None and duration:
            self.clock.advance(duration)
        signed = events.sign_event(dict(unsigned), SECRET_KEY)
        if self.tamper is not None:
            signed = self.tamper(signed)
        on_success(signed)

    def release(self) -> None:
        unsigned, on_success, _on_failure = self.pending.pop(0)
        self._answer(unsigned, on_success)

    def refuse(self, reason: str) -> None:
        _unsigned, _on_success, on_failure = self.pending.pop(0)
        on_failure(reason)


# --------------------------------------------------------------------------- #
# A server that checks                                                         #
# --------------------------------------------------------------------------- #

def config_data(**overrides) -> dict:
    data = {
        "fee": 21,
        "currency": "CHF",
        "year": 2026,
        "statutes": {
            "url": "https://einundzwanzig.space/files/Statuten_v1.3.pdf",
            "version": "1.3",
            "adopted_at": "2024-04-20",
        },
        "application": {
            "required_fields": ["statutes_accepted"],
            "optional_fields": ["application_text", "email", "no_email", "nip05_handle"],
            "application_text_max_length": 2000,
        },
    }
    data.update(overrides)
    return data


def membership_data(status: str = "awaiting_payment", *, paid: bool = False, **overrides) -> dict:
    data = {
        "pubkey": PUBKEY,
        "association_status": "PASSIVE",
        "association_status_value": 2,
        "membership_status": status,
        "statutes_accepted_at": "2026-02-14T09:31:07+00:00",
        "applied_at": "2026-02-14T09:31:07+00:00",
        "current_year": {
            "year": 2026,
            "fee": 21,
            "currency": "CHF",
            "paid": paid,
            "receipt_url": "https://pay.einundzwanzig.space/i/abc/receipt" if paid else None,
        },
    }
    data.update(overrides)
    return data


def invoice_data(*, paid: bool = False, created: bool = True,
                 checkout_url: Optional[str] = "https://pay.einundzwanzig.space/i/abc",
                 bolt11: Optional[str] = None) -> dict:
    return {
        "checkout_url": checkout_url,
        "bolt11": bolt11,
        "created": created,
        "payment": {
            "year": 2026,
            "amount": 21,
            "currency": "CHF",
            "paid": paid,
            "receipt_url": "https://pay.einundzwanzig.space/i/abc/receipt" if paid else None,
        },
    }


class FakeMembershipServer:
    """Answers the main surface the way the spec says, checking first.

    ``routes`` maps ``"METHOD /path"`` (path under the prefix) to a reply
    factory taking ``(body_bytes, event)``; the default routes answer
    with plausible data. Every refusal is the same 401 the real server
    gives, with the reason kept in :attr:`refusals` for assertions.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.seen_ids: set = set()
        self.status = {"service": "myeditor-sidecar", "membership": True}
        self.accepted: List[dict] = []
        self.refusals: List[str] = []
        self.bodies: List[Optional[bytes]] = []
        self.routes: Dict[str, Callable[[Optional[bytes], Optional[dict]], FakeReply]] = {
            "GET /config": lambda b, e: data_reply(config_data()),
            "GET /me": lambda b, e: data_reply(membership_data()),
            "POST /applications": lambda b, e: data_reply(membership_data(), status=201),
            "POST /payments/2026/invoice": lambda b, e: data_reply(invoice_data()),
            "POST /payments/2026/refresh": lambda b, e: data_reply(invoice_data(created=False)),
            "GET /payments": lambda b, e: data_reply([invoice_data()["payment"]]),
            "GET /export": lambda b, e: data_reply({
                "subject": {"pubkey": PUBKEY, "npub": None},
                "membership_status": "none",
                "member": None,
                "payments": [],
                "membership_grants": [],
                "nostr_profile": None,
            }),
            "DELETE /me": lambda b, e: data_reply({"erased": True, "retained_payments": 1}),
        }

    def _refuse(self, reason: str) -> FakeReply:
        self.refusals.append(reason)
        return json_reply({"message": "Unauthenticated."}, status=401)

    def __call__(self, verb: str, request, body: Optional[bytes]) -> FakeReply:
        """The service and the association as one: requests arrive at the
        service, and their signatures must name the association's URL."""
        url = request.url().toString()
        if url == SERVICE + "/status":
            return json_reply(self.status)
        assert url.startswith(SERVICE_PREFIX), url
        path = url[len(SERVICE_PREFIX):]
        self.bodies.append(body)

        if header(request, "X-Api-Key") is not None:
            return self._refuse("a client key from the app")   # it must never have one
        if path == "/config" and verb == "GET":
            return self.routes["GET /config"](body, None)

        if nip98.has_body(body) and header(request, "Content-Type") != b"application/json":
            return json_reply({"message": "Unsupported Media Type"}, status=415)
        try:
            event = nip98.check_auth_header(header(request, "Authorization"),
                                            url=PREFIX + path, method=verb, body=body,
                                            now=self.clock())
        except nip98.AuthRefused as refusal:
            return self._refuse(refusal.reason)
        if event["id"] in self.seen_ids:
            return self._refuse("replay")
        self.seen_ids.add(event["id"])
        self.accepted.append(event)

        route = self.routes.get(f"{verb} {path}")
        if route is None:
            return json_reply({"message": "Not Found"}, status=404)
        return route(body, event)
