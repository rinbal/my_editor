# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Qt client for a STANDUP submission server (editorial contract 12).

:class:`StandupApi` runs on the call pipeline every signed API of the app
shares (``nostr/signed_api.py``: a fresh NIP-98 signature right before
sending, the exact bytes hashed and sent, a reply read within a limit,
exactly one callback). What is STANDUP's own:

- **Where requests go**: one client per server and account, the address
  a verified key event names (``servers.py``).
- **Three ways to authorize** (12.1). A signed action is a NIP-98 request
  that always carries a ``payload`` tag (contract 10), so its body is
  never empty, ``{}`` at least. Reading what needs a person goes with a
  session token (``POST /api/session`` once, then ``Bearer``), so
  following a submission does not prompt the signer app for every read;
  an expired or unknown token is replaced once, by itself. The status
  key lookup and the public endpoints carry nothing.
- **Answers** are parsed once, into the types of ``answers.py`` and
  ``views.py``; a malformed one is a bad response. **Errors** are
  :class:`~.errors.StandupError`, with ``maybe_stored`` when the request
  may have arrived although no answer did, the case contract 12.4
  resolves with a lookup ("No answer after sending").
- **Receipts** are compared with what was sent before anything else sees
  them (:func:`~.views.check_receipt`): :meth:`StandupApi.submit` answers
  Confirmed, BlobsMissing or Mismatch, never a bare "ok".

The token lives in memory only, for this client: it is never written
anywhere, and :meth:`StandupApi.stop` (sign-out, another account)
forgets it.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlencode

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply

from .. import events as nostr_events
from ..http_answers import clean_text, parse_retry_after
from ..signed_api import Call, SignedJsonApi, SignFn, answer_later, json_body, reply_header
from . import answers, formats, views
from .errors import Code, StandupError, error_from_answer, refused
from .formats import FormatError, need
from .servers import usable_server_url
from .submission import submission_body_problem

# Answers larger than this are refused before they are read whole.
MAX_ANSWER_BYTES = 1024 * 1024
# A lookup answers up to 50 submissions with their new conversation
# items, each up to 96 KiB.
MAX_LOOKUP_ANSWER_BYTES = 16 * 1024 * 1024
# A submission's detail holds every revision's envelope (4 MiB each).
MAX_DETAIL_ANSWER_BYTES = 64 * 1024 * 1024
# Lists of views, publications or keys.
MAX_LIST_ANSWER_BYTES = 4 * 1024 * 1024
# A session is replaced this long before the server would refuse it.
SESSION_MARGIN_SECONDS = 60

# 12.4: the views a publication's submissions are listed by.
SITE_VIEWS = ("mine", "open", "release", "done")
# PATCH .../members/me changes only these.
MEMBERSHIP_FIELDS = frozenset({"away_until", "hand_back", "notices"})
MESSAGE_CHANNELS = ("conversation", "internal")

# Transport failures that happen before a request is written: no
# connection, no name, no TLS, no proxy, a scheme Qt has no way to send.
# The server never saw it. Everything else (a network that went away, a
# session of the system's network that failed) may come after the
# request was written, so it may have been stored and goes to the lookup.
_NOT_SENT = frozenset({
    QNetworkReply.NetworkError.ConnectionRefusedError,
    QNetworkReply.NetworkError.HostNotFoundError,
    QNetworkReply.NetworkError.SslHandshakeFailedError,
    QNetworkReply.NetworkError.ProxyConnectionRefusedError,
    QNetworkReply.NetworkError.ProxyNotFoundError,
    QNetworkReply.NetworkError.ProxyAuthenticationRequiredError,
    QNetworkReply.NetworkError.UnknownProxyError,
    QNetworkReply.NetworkError.ProtocolUnknownError,
})


class Auth:
    """How a call is authorized (12.1)."""

    NONE = "none"
    SIGNED = "signed"
    SESSION = "session"


@dataclass
class StandupCall(Call):
    """A call and what STANDUP keeps about it."""

    auth: str = Auth.NONE
    # The token a call carries instead of the current session's.
    bearer: str = field(default="", repr=False)
    # The token the call was sent with, so a late refusal of it does not
    # throw away a session opened since.
    carried: str = field(default="", repr=False)
    session_retried: bool = False
    # The answer's status (a receipt's 200 says "the server had it").
    status: int = 0
    # The answer is in the contract's envelope (``"ok": true``).
    in_envelope: bool = True


class StandupApi(SignedJsonApi):
    """One STANDUP server, for one account (the ``sign`` callable signs
    as that account). Every method takes ``on_success`` and
    ``on_failure``; exactly one fires, never inside a Qt slot's raise.
    A malformed submission id or argument raises ValueError at once (a
    bug of the caller, not a failure of the server).

    Seams: ``sign``, ``nam``, ``clock``; ``local_http`` overrides the
    developer setting (tests).
    """

    max_response_bytes = MAX_ANSWER_BYTES
    call_type = StandupCall

    def __init__(self, sign: SignFn, *, server: str, nam: Optional[QNetworkAccessManager] = None,
                 clock: Optional[Callable[[], float]] = None, local_http: Optional[bool] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(sign, nam=nam, clock=clock, parent=parent)
        self._server = usable_server_url(server, local_http=local_http)
        self._session: Optional[answers.Session] = None
        self._session_call: Optional[StandupCall] = None
        self._waiting: List[StandupCall] = []

    @property
    def server(self) -> str:
        return self._server

    def configured(self) -> bool:
        return bool(self._server)

    def url_for(self, path: str) -> str:
        return self._server + path

    # -- account scope -------------------------------------------------------

    def cancel(self) -> None:
        super().cancel()
        self._waiting.clear()
        self._session_call = None

    def stop(self) -> None:
        """Sign-out or another account: drop every call, forget the token."""
        self.cancel()
        self._session = None

    @property
    def has_session(self) -> bool:
        return self._session_usable()

    # -- the pipeline's questions -------------------------------------------

    def _error(self, code: str, **fields) -> StandupError:
        return StandupError(code, **fields)

    def _unavailable(self, call: StandupCall) -> Optional[StandupError]:
        return None if self._server else StandupError(Code.UNAVAILABLE)

    def _prepare_request(self, call: StandupCall, request) -> None:
        if call.auth != Auth.SESSION:
            return
        token = call.bearer or (self._session.token if self._session else "")
        call.carried = token
        if token:
            request.setRawHeader(b"Authorization", b"Bearer " + token.encode("ascii"))

    def _transport_error(self, error) -> StandupError:
        return replace(super()._transport_error(error), maybe_stored=error not in _NOT_SENT)

    def _read(self, call: StandupCall, reply, flags: dict, limit: int):
        outcome = super()._read(call, reply, flags, limit)
        if isinstance(outcome, StandupError) and not outcome.maybe_stored:
            status = outcome.status or 0
            # An answer that came but could not be used (2xx unreadable,
            # too large, a 5xx from a proxy behind which the server may
            # have stored it): the request arrived.
            if (200 <= status < 300 or status >= 500
                    or (outcome.code == Code.BAD_RESPONSE and status == 0)):
                outcome = replace(outcome, maybe_stored=True)
        return outcome

    def _parse_success(self, call: StandupCall, status: int, raw: bytes):
        call.status = status
        try:
            data = formats.strict_json(raw, limit=len(raw))
            if call.in_envelope:
                need(isinstance(data, dict) and data.get("ok") is True,
                     "the answer is not a success")
            return (call.parse(data),)
        except FormatError as exc:
            return StandupError(Code.BAD_RESPONSE, status=status,
                                message=clean_text(str(exc)), maybe_stored=True)

    def _http_error(self, status: int, raw: bytes, reply) -> StandupError:
        retry_after = None
        if status in (429, 503):
            retry_after = parse_retry_after(reply_header(reply, "Retry-After"),
                                            now=self._clock())
        return error_from_answer(status, raw, retry_after=retry_after)

    def _retry(self, call: StandupCall, error: StandupError, stale: bool) -> bool:
        if call.auth == Auth.SESSION and not call.session_retried and (
                error.code == Code.SESSION_EXPIRED
                or (error.code == Code.AUTH_INVALID and error.reason == "session")):
            # The token ran out or the server no longer knows it: one new
            # session, then the same call again. Only the token this call
            # carried is forgotten: one opened meanwhile is kept and used.
            call.session_retried = True
            if self._carries_current(call):
                self._session = None
            if self._session_usable():
                self._send(call, None)
            else:
                self._with_session(call)
            return True
        if (call.auth == Auth.SIGNED and stale and not call.resigned
                and error.code == Code.AUTH_INVALID):
            # A signature that took most of its 60 seconds on the phone
            # and expired on the way: signed once more, once.
            call.resigned = True
            self._sign_and_send(call)
            return True
        return False

    # -- calls ---------------------------------------------------------------

    def _build(self, method: str, path: str, payload: Optional[Mapping[str, Any]], auth: str,
               parse: Optional[Callable[[Any], Any]], on_success, on_failure, *,
               limit: Optional[int] = None, in_envelope: bool = True) -> StandupCall:
        if auth == Auth.SIGNED and payload is None:
            payload = {}   # contract 10: a signed action always has a payload tag
        body = None if payload is None else json_body(payload)
        return self._new_call(method, path, body, auth == Auth.SIGNED, parse, on_success,
                              on_failure, auth=auth, max_response_bytes=limit,
                              in_envelope=in_envelope)

    def _dispatch(self, call: StandupCall) -> StandupCall:
        if call.auth != Auth.SESSION:
            self._run(call)
            return call
        problem = self._unavailable(call)
        if problem is not None:
            answer_later(self._fail, call, problem)
        elif self._session_usable():
            self._send(call, None)
        else:
            self._with_session(call)
        return call

    def _call(self, method: str, path: str, payload: Optional[Mapping[str, Any]], auth: str,
              parse: Callable[[Any], Any], on_success, on_failure, *,
              limit: Optional[int] = None) -> StandupCall:
        return self._dispatch(self._build(method, path, payload, auth, parse, on_success,
                                          on_failure, limit=limit))

    def _refuse(self, method: str, path: str, problem: str, on_success,
                on_failure) -> StandupCall:
        """A request that is not sent: ``on_failure`` with why, on the
        next turn."""
        call = self._build(method, path, None, Auth.NONE, None, on_success, on_failure)
        answer_later(self._fail, call, refused(problem))
        return call

    def _carries_current(self, call: StandupCall) -> bool:
        return (self._session is not None and bool(call.carried)
                and hmac.compare_digest(call.carried, self._session.token))

    def _session_usable(self) -> bool:
        return (self._session is not None
                and self._session.expires_at - SESSION_MARGIN_SECONDS > self._clock())

    def _with_session(self, call: StandupCall) -> None:
        """Send ``call`` once a session is open; one opens at a time."""
        self._waiting.append(call)
        if self._session_call is not None and self._live(self._session_call):
            return

        def opened(_session: answers.Session) -> None:
            self._session_call = None
            waiting, self._waiting = self._waiting, []
            for item in waiting:
                if self._live(item):
                    self._send(item, None)

        def failed(error: StandupError) -> None:
            self._session_call = None
            waiting, self._waiting = self._waiting, []
            for item in waiting:
                self._fail(item, error)

        self._session_call = self.open_session(opened, failed)

    # -- sessions (12.1) -----------------------------------------------------

    def open_session(self, on_success: Callable[[answers.Session], None],
                     on_failure: Callable[[StandupError], None]) -> StandupCall:
        """``POST /api/session``: one signature, a 12-hour token."""
        call = self._build("POST", "/api/session", {}, Auth.SIGNED, None,
                           on_success, on_failure)

        def parse(data: Mapping[str, Any]) -> answers.Session:
            self._session = answers.parse_session(data, signed_by=call.signed_by,
                                                  now=self._clock())
            return self._session

        call.parse = parse
        return self._dispatch(call)

    def close_session(self, on_done: Callable[[], None]) -> None:
        """``DELETE /api/session``: the token is forgotten here at once,
        and the server is asked to revoke it; ``on_done`` either way, on a
        later turn. The revocation is not cancelled by :meth:`stop` (the
        sign-out that follows must not keep the token alive on the
        server), and ``on_done`` still comes."""
        session, self._session = self._session, None
        if session is None or not self._server:
            answer_later(on_done)
            return
        call = self._build("DELETE", "/api/session", None, Auth.SESSION,
                           answers.parse_acknowledged, lambda _v: on_done(),
                           lambda _e: on_done())
        call.bearer = session.token
        call.cancellable = False
        self._send(call, None)

    # -- instance and publications (12.3) ------------------------------------

    def instance(self, on_success: Callable[[answers.Instance], None],
                 on_failure) -> StandupCall:
        return self._call("GET", "/api/editorial/instance", None, Auth.NONE,
                          answers.parse_instance, on_success, on_failure)

    def destinations(self, on_success: Callable[[Tuple[answers.Listing, ...]], None],
                     on_failure) -> StandupCall:
        """``GET /api/submissions/destinations``: the open publications
        as the server lists them, for ``trust`` to verify."""
        return self._call("GET", "/api/submissions/destinations", None, Auth.NONE,
                          answers.parse_listings, on_success, on_failure,
                          limit=MAX_LIST_ANSWER_BYTES)

    @staticmethod
    def _site(address: str) -> str:
        pubkey, site_d = formats.parse_address(address)
        return "/api/sites/" + formats.route_key(pubkey, site_d)

    def site_settings(self, address: str, on_success: Callable[[answers.SiteSettings], None],
                      on_failure) -> StandupCall:
        return self._call("GET", self._site(address) + "/settings", None, Auth.NONE,
                          answers.parse_site_settings, on_success, on_failure)

    def put_site_settings(self, address: str, *, key_event: Optional[dict],
                          settings_event: Optional[dict], owner_wrap: Optional[dict] = None,
                          on_success: Callable[[answers.SiteSettings], None],
                          on_failure) -> StandupCall:
        """``PUT .../settings``. The first one registers the publication
        (201) and MUST carry ``owner_wrap``, epoch 1's key wrapped to the
        owner (12.3), so the keyring is never the only copy."""
        return self._call("PUT", self._site(address) + "/settings",
                          {"key_event": key_event, "settings_event": settings_event,
                           "owner_wrap": owner_wrap},
                          Auth.SIGNED, answers.parse_site_settings, on_success, on_failure)

    def members(self, address: str, on_success: Callable[[Tuple[answers.Member, ...]], None],
                on_failure) -> StandupCall:
        return self._call("GET", self._site(address) + "/members", None, Auth.SESSION,
                          answers.parse_members, on_success, on_failure,
                          limit=MAX_LIST_ANSWER_BYTES)

    def invite_member(self, address: str, *, pubkey: str, role: str, sees: str,
                      wraps: Sequence[Mapping[str, Any]], history: Optional[Mapping[str, str]],
                      on_success: Callable[[answers.Member], None], on_failure) -> StandupCall:
        """``history`` is the member's own chain of older epoch keys
        (12.3): for ``"all"`` once the epoch is above 1, never for
        ``"since_join"``, who must not get any older secret."""
        if sees == "since_join" and history is not None:
            raise ValueError("a member who sees only what follows gets no history chain")
        return self._call("POST", self._site(address) + "/members",
                          {"pubkey": pubkey, "role": role, "sees": sees, "wraps": list(wraps),
                           "history": history},
                          Auth.SIGNED, answers.parse_one_member, on_success, on_failure)

    def accept_membership(self, address: str, on_success: Callable[[answers.Member], None],
                          on_failure) -> StandupCall:
        return self._call("POST", self._site(address) + "/members/accept", {}, Auth.SIGNED,
                          answers.parse_one_member, on_success, on_failure)

    def remove_member(self, address: str, *, pubkey: str, key_event: Mapping[str, Any],
                      wraps: Mapping[str, Mapping[str, str]],
                      on_success: Callable[[int], None], on_failure) -> StandupCall:
        """Remove ``pubkey`` and start the next epoch, whose ``wraps``
        cover every member who keeps access. ``on_success(epoch)``: the
        epoch now current."""
        return self._call("POST", self._site(address) + "/members/remove",
                          {"pubkey": pubkey, "epoch": _new_epoch(key_event, wraps)},
                          Auth.SIGNED, answers.parse_epoch_number, on_success, on_failure)

    def update_my_membership(self, address: str, changes: Mapping[str, Any],
                             on_success: Callable[[answers.Member], None],
                             on_failure) -> StandupCall:
        """``PATCH .../members/me`` with some of :data:`MEMBERSHIP_FIELDS`."""
        unknown = set(changes) - MEMBERSHIP_FIELDS
        if unknown:
            raise ValueError(f"a membership has no {', '.join(sorted(unknown))}")
        return self._call("PATCH", self._site(address) + "/members/me", dict(changes),
                          Auth.SIGNED, answers.parse_one_member, on_success, on_failure)

    def new_epoch(self, address: str, *, key_event: Mapping[str, Any],
                  wraps: Mapping[str, Mapping[str, str]],
                  on_success: Callable[[int], None], on_failure) -> StandupCall:
        return self._call("POST", self._site(address) + "/epochs",
                          _new_epoch(key_event, wraps), Auth.SIGNED,
                          answers.parse_epoch_number, on_success, on_failure)

    def keys(self, address: str, on_success: Callable[[answers.EpochKeys], None],
             on_failure) -> StandupCall:
        return self._call("GET", self._site(address) + "/keys", None, Auth.SESSION,
                          answers.parse_epoch_keys, on_success, on_failure,
                          limit=MAX_LIST_ANSWER_BYTES)

    def keyring(self, on_success: Callable[[answers.Keyring], None],
                on_failure) -> StandupCall:
        return self._call("GET", "/api/me/keyring", None, Auth.SESSION, answers.parse_keyring,
                          on_success, on_failure, limit=MAX_LOOKUP_ANSWER_BYTES)

    def put_keyring(self, *, expected_version: int, pages: Sequence[str],
                    on_success: Callable[[int], None], on_failure) -> StandupCall:
        """``on_success(version)``; a 409 ``conflict`` when another device
        wrote first. A page longer than the server takes is not sent."""
        if any(not isinstance(page, str) or len(page) > formats.MAX_KEYRING_PAGE_CHARS
               for page in pages):
            return self._refuse("PUT", "/api/me/keyring",
                                f"a keyring page is at most {formats.MAX_KEYRING_PAGE_CHARS} "
                                "characters", on_success, on_failure)
        return self._call("PUT", "/api/me/keyring",
                          {"expected_version": expected_version, "pages": list(pages)},
                          Auth.SIGNED, answers.parse_keyring_version, on_success, on_failure)

    def bote_pubkey(self, on_success: Callable[[str], None], on_failure) -> StandupCall:
        """``/.well-known/nostr.json?name=bote``: the notice account's key."""
        return self._dispatch(self._build(
            "GET", "/.well-known/nostr.json?name=bote", None, Auth.NONE,
            answers.parse_notice_pubkey, on_success, on_failure, in_envelope=False))

    # -- submissions (12.4) --------------------------------------------------

    def submit(self, body: Mapping[str, Any], on_success: Callable[[Any], None],
               on_failure: Callable[[StandupError], None], *,
               on_sending: Optional[Callable[[], None]] = None) -> StandupCall:
        """``POST /api/submissions`` with ``body`` (see
        :func:`~.submission.submission_body`).

        ``on_success`` gets :class:`~.views.Confirmed`, :class:`~.views.BlobsMissing`
        or :class:`~.views.Mismatch`. Sending the same body again (after
        uploading missing files, or after no answer) is safe: the server
        answers the stored receipt (criterion 5). ``on_sending`` is told
        once the request is signed and written to the network; until then
        :meth:`drop` keeps it from being sent at all.
        """
        problem = submission_body_problem(body)
        if problem:
            return self._refuse("POST", "/api/submissions", problem, on_success, on_failure)
        call = self._build("POST", "/api/submissions", dict(body), Auth.SIGNED, None,
                           on_success, on_failure)
        call.on_sending = on_sending
        call.parse = lambda data: views.check_receipt(body, data.get("receipt"),
                                                      now=self._clock(),
                                                      repeat=call.status == 200)
        return self._dispatch(call)

    def lookup(self, items: Sequence[Tuple[str, int]],
               on_success: Callable[[answers.LookupAnswer], None],
               on_failure) -> StandupCall:
        """``POST /api/submissions/lookup`` with ``(status_key, since)``
        pairs, at most 50, and no signature. Internal notes are dropped
        whatever the server sends."""
        items = list(items)
        path = "/api/submissions/lookup"
        if not items or len(items) > formats.MAX_LOOKUP_ITEMS:
            return self._refuse("POST", path, f"a lookup asks for 1 to "
                                f"{formats.MAX_LOOKUP_ITEMS} submissions",
                                on_success, on_failure)
        if not all(formats.is_hex64(key) and formats.is_int(since) for key, since in items):
            return self._refuse("POST", path, "a status key is malformed",
                                on_success, on_failure)
        asked = {formats.status_key_hash(key): key for key, _since in items}
        return self._call("POST", path,
                          {"items": [{"status_key": k, "since": s} for k, s in items]},
                          Auth.NONE, lambda data: answers.parse_lookup(data, asked=asked),
                          on_success, on_failure, limit=MAX_LOOKUP_ANSWER_BYTES)

    def submission(self, submission_id: str,
                   on_success: Callable[[answers.SubmissionDetail], None],
                   on_failure, *, since: Optional[int] = None,
                   revision: Optional[int] = None) -> StandupCall:
        """``GET /api/submissions/<sid>?since=seq&revision=n`` with the
        session: the latest revision, or only revision ``revision``. An
        answer with ``more`` has timeline items beyond ``since`` waiting:
        ask again from its last ``seq`` (``watcher.read_submission`` does)."""
        path = f"/api/submissions/{_sid(submission_id)}"
        query = {}
        if since is not None:
            if not formats.is_int(since):
                raise ValueError("since is a seq")
            query["since"] = since
        if revision is not None:
            if not formats.is_int(revision, low=1):
                raise ValueError("a revision number starts at 1")
            query["revision"] = revision
        if query:
            path += "?" + urlencode(query)
        return self._call("GET", path, None, Auth.SESSION, answers.parse_submission_detail,
                          on_success, on_failure, limit=MAX_DETAIL_ANSWER_BYTES)

    def send_message(self, submission_id: str, wrap: Mapping[str, Any], *, kind_hint: str,
                     on_success: Callable[[int], None], on_failure,
                     channel: str = "conversation") -> StandupCall:
        """``POST .../messages`` with the session: the gift wrap and what
        kind of item it is (``kind_hint``, so a reaction sends no notice).
        ``on_success(seq)``: only a ``seq`` means the server has it."""
        path = f"/api/submissions/{_sid(submission_id)}/messages"
        if kind_hint not in views.KIND_HINTS:
            raise ValueError("unknown kind of message")
        if channel not in MESSAGE_CHANNELS:
            raise ValueError("unknown channel")
        problem = None
        if not nostr_events.is_verified_event(wrap, kind=1059):
            problem = "not a gift wrap"
        elif len(formats.canonical_bytes(wrap)) > formats.MAX_WRAP_EVENT_BYTES:
            problem = "the wrapped message is larger than the server takes"
        if problem:
            return self._refuse("POST", path, problem, on_success, on_failure)
        return self._call("POST", path, {"channel": channel, "wrap": dict(wrap),
                                         "kind_hint": kind_hint},
                          Auth.SESSION, answers.parse_seq, on_success, on_failure)

    def _action(self, submission_id: str, name: str, payload: Mapping[str, Any], parse,
                on_success, on_failure) -> StandupCall:
        sid = _sid(submission_id)
        return self._call("POST", f"/api/submissions/{sid}/{name}",
                          {"submission_id": sid, **payload}, Auth.SIGNED, parse,
                          on_success, on_failure)

    def decision(self, submission_id: str, *, outcome: str, expected_revision: int,
                 message_wrap: Optional[dict] = None, split_wrap: Optional[dict] = None,
                 split_rumor_id: Optional[str] = None, section: Optional[str] = None,
                 on_success: Callable[[answers.Recorded], None], on_failure) -> StandupCall:
        return self._action(submission_id, "decision", {
            "outcome": outcome, "expected_revision": expected_revision,
            "message_wrap": message_wrap, "split_wrap": split_wrap,
            "split_rumor_id": split_rumor_id, "section": section,
        }, answers.parse_recorded, on_success, on_failure)

    def confirm(self, submission_id: str, *, revision_hash: str, confirms: Sequence[str],
                split_rumor_id: Optional[str] = None,
                on_success: Callable[[views.SubmissionView], None], on_failure) -> StandupCall:
        return self._action(submission_id, "confirmation", {
            "revision_hash": revision_hash, "split_rumor_id": split_rumor_id,
            "confirms": list(confirms)}, answers.parse_view, on_success, on_failure)

    def responsible(self, submission_id: str, *, action: str, to: Optional[str] = None,
                    on_success: Callable[[views.SubmissionView], None],
                    on_failure) -> StandupCall:
        return self._action(submission_id, "responsible", {"action": action, "to": to},
                            answers.parse_view, on_success, on_failure)

    def withdraw(self, submission_id: str, on_success: Callable[[views.SubmissionView], None],
                 on_failure) -> StandupCall:
        return self._action(submission_id, "withdraw", {}, answers.parse_view,
                            on_success, on_failure)

    def published(self, submission_id: str, *, naddr: str, event: dict,
                  on_success: Callable[[views.SubmissionView], None],
                  on_failure) -> StandupCall:
        return self._action(submission_id, "published", {"naddr": naddr, "event": event},
                            answers.parse_view, on_success, on_failure)

    def placed(self, submission_id: str, *, site_event: dict, section: Optional[str] = None,
               on_success: Callable[[views.SubmissionView], None], on_failure) -> StandupCall:
        return self._action(submission_id, "placed",
                            {"site_event": site_event, "section": section},
                            answers.parse_view, on_success, on_failure)

    def correction(self, submission_id: str, *, revision: int, revision_hash: str,
                   envelope: str, envelope_sha256: str, wraps: Mapping[str, Any],
                   blobs: Sequence[Mapping[str, Any]],
                   on_success: Callable[[answers.Recorded], None], on_failure) -> StandupCall:
        """A Redaktion member's revision, as a revision body (12.4)."""
        return self._action(submission_id, "corrections", {
            "revision": revision, "revision_hash": revision_hash, "envelope": envelope,
            "envelope_sha256": envelope_sha256, "wraps": dict(wraps),
            "blobs": [dict(b) for b in blobs]}, answers.parse_recorded, on_success, on_failure)

    def remind(self, submission_id: str, *, to: str, on_success: Callable[[None], None],
               on_failure) -> StandupCall:
        return self._action(submission_id, "remind", {"to": to}, answers.parse_acknowledged,
                            on_success, on_failure)

    def request_reupload(self, submission_id: str, *, sha256: str,
                         on_success: Callable[[None], None], on_failure) -> StandupCall:
        return self._action(submission_id, "reupload", {"sha256": sha256},
                            answers.parse_acknowledged, on_success, on_failure)

    def propose(self, address: str, *, naddr: str, event: dict, p_sub: str,
                wraps: Mapping[str, Any], title: str,
                on_success: Callable[[views.SubmissionView], None], on_failure) -> StandupCall:
        return self._call("POST", self._site(address) + "/proposals",
                          {"naddr": naddr, "event": event, "p_sub": p_sub,
                           "wraps": dict(wraps), "title": title},
                          Auth.SIGNED, answers.parse_view, on_success, on_failure)

    def site_submissions(self, address: str, *, view: str = "mine",
                         cursor: Optional[str] = None, q: Optional[str] = None,
                         on_success: Callable[[answers.SubmissionPage], None],
                         on_failure) -> StandupCall:
        if view not in SITE_VIEWS:
            raise ValueError(f"the view is one of {', '.join(SITE_VIEWS)}")
        query = {"view": view}
        if cursor:
            query["cursor"] = cursor
        if q:
            query["q"] = q
        return self._call("GET", self._site(address) + "/submissions?" + urlencode(query),
                          None, Auth.SESSION, answers.parse_submission_page, on_success,
                          on_failure, limit=MAX_LIST_ANSWER_BYTES)

    def my_editorial(self, on_success: Callable[[answers.Editorial], None],
                     on_failure) -> StandupCall:
        return self._call("GET", "/api/me/editorial", None, Auth.SESSION,
                          answers.parse_editorial, on_success, on_failure,
                          limit=MAX_LIST_ANSWER_BYTES)

    def my_submissions(self, on_success: Callable[[Tuple[views.SubmissionView, ...]], None],
                       on_failure) -> StandupCall:
        return self._call("GET", "/api/me/submissions", None, Auth.SESSION,
                          answers.parse_views, on_success, on_failure,
                          limit=MAX_LIST_ANSWER_BYTES)


def _sid(value: str) -> str:
    if not formats.is_hex64(value):
        raise ValueError("a submission id is 64 hex digits")
    return value



def _new_epoch(key_event: Mapping[str, Any],
               wraps: Mapping[str, Mapping[str, str]]) -> Dict[str, Any]:
    """``NewEpoch`` (12.3): the key event and a wrap per member who keeps
    access. A rotation carries no history: those members hold the older
    secrets already."""
    return {"key_event": dict(key_event), "wraps": {pubkey: dict(wrap)
                                                    for pubkey, wrap in wraps.items()}}

__all__ = ["Auth", "MAX_ANSWER_BYTES", "StandupApi", "StandupCall"]
