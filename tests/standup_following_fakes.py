# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A world for the tests of following a submission: the checking fake
server, one transport, the account's real submissions list and read
state on one fake relay (so a second :class:`TestAccount` of the same
person is that person after a restart, or on another device), and the
real :class:`~nostr.ui.standup_following.Following`.

Waits are held until a test runs them (:class:`Later`); nothing here
reads a wall clock (lesson 34)."""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from typing import List, Optional

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication

from nostr import crypto, events
from nostr.standup import conversation, formats, trust
from nostr.standup import crypto as scrypto
from nostr.standup.client import StandupApi
from nostr.standup.entries import SubmissionEntry
from nostr.standup.link_names import SubmittedMarks
from nostr.standup.lists import ReadState, SubmissionsList
from nostr.standup.opening import MediaKeys
from nostr.ui.standup_account import Account
from nostr.ui.standup_following import Following
from tests.membership_fakes import FakeClock, FakeTimer
from tests.outbox_fakes import FakeRelayDirectory
from tests.standup_fakes import (
    KEY_EVENT, SERVER, FakeStandupServer, KeySigner, SignAs, StandupNam, submission_for,
)
from tests.standup_vectors import ADDRESS, NOW, pk, sk
from tests.test_encrypted_lists import Relay
from tests.test_imports_subscriptions import FakeSessionPool


class Later:
    """``later(ms, call)``: a turn (0 ms) as Qt's, a wait held until the
    test runs it."""

    def __init__(self) -> None:
        self.waiting: List[tuple] = []

    def __call__(self, ms, call) -> None:
        if ms <= 0:
            QTimer.singleShot(0, call)
        else:
            self.waiting.append((ms, call))

    def run(self) -> List[int]:
        ran = []
        while self.waiting:
            ms, call = self.waiting.pop(0)
            ran.append(ms)
            call()
        return ran


class AccountSigner:
    """``Account.signer``: seals with the person's key, opens a wrap."""

    def __init__(self, signer: KeySigner) -> None:
        self.key = signer
        self.held: Optional[list] = None        # a list: requests wait for release()

    def sign_event(self, unsigned, *, on_success, on_failure) -> None:
        if self.held is not None:
            self.held.append((unsigned, on_success, on_failure))
        else:
            self.key.sign_event(unsigned, on_success, on_failure)

    def release(self) -> None:
        waiting, self.held = self.held or [], None
        for unsigned, on_success, on_failure in waiting:
            self.key.sign_event(unsigned, on_success, on_failure)

    def nip44_decrypt(self, peer, ciphertext, *, on_success, on_failure) -> None:
        self.key.nip44_decrypt(peer, ciphertext, on_success, on_failure)


class TestAccount(QObject):
    """What ``standup_account.Account`` holds, with the real lists on
    the world's relay and the real client on its transport."""

    __test__ = False

    def __init__(self, world: "FollowWorld", who: str) -> None:
        super().__init__()
        self.profile = SimpleNamespace(user_pubkey=pk(who), bunker_relays=[])
        self.writer = pk(who)
        self.key = KeySigner(sk(who))
        self.sign = SignAs(sk(who), clock=world.clock)
        self.api = StandupApi(self.sign, server=SERVER, nam=world.nam, clock=world.clock,
                              parent=self)
        self.clients = {}
        lists = dict(session_pool=FakeSessionPool(self.key),
                     relay_directory=FakeRelayDirectory(), query=world.relay,
                     publisher=world.relay, clock=world.clock)
        self.submissions = SubmissionsList(self.profile, **lists)
        self.read_state = ReadState(self.profile, **lists)
        self.media_keys = MediaKeys()
        self.epochs = trust.EpochStore()
        self.relay_lookup = lambda _pubkey, _d, on_done: on_done([])
        self.destinations = trust.Destinations(self.api, self.relay_lookup, self.epochs,
                                               parent=self)
        self.signer = AccountSigner(self.key)
        self.runs: list = []
        self.stopped = False
        self.list_problem = ""
        self._reading = None
        self._later = world.later
        self.read_whole = MethodType(Account.read_whole, self)

    def client_for(self, server: str) -> StandupApi:
        return self.api

    def let_go(self) -> None:
        """As StandupWriter.stop lets an account go."""
        self.stopped = True
        self.api.stop()
        self.submissions.stop()
        self.read_state.stop()
        self.media_keys.clear()


class FollowWorld:
    def __init__(self) -> None:
        self.clock = FakeClock(NOW)
        self.server = FakeStandupServer(self.clock)
        self.server.publication().members[pk("editor")] = "redaktion"
        self.nam = StandupNam(responder=self.server)
        self.relay = Relay()
        self.later = Later()
        self.editor = StandupApi(SignAs(sk("editor"), clock=self.clock), server=SERVER,
                                 nam=self.nam, clock=self.clock)
        self.news: list = []
        self.discarded: list = []

    def settle(self) -> None:
        """Every answer on its way, and what it starts."""
        for _ in range(40):
            self.nam.settle()
            QApplication.processEvents()

    def account(self, who: str = "writer") -> TestAccount:
        return TestAccount(self, who)

    def following(self, account: TestAccount, *, start: bool = True) -> Following:
        marks = SubmittedMarks(kept=False)

        def discard(entry, on_done) -> None:
            self.discarded.append(entry)
            account.submissions.forget(entry, lambda result: on_done(result.stored))

        following = Following(account, marks=marks, on_listed=lambda _read: None,
                              discard=discard, later=self.later, clock=self.clock,
                              timer=FakeTimer(), parent=account)
        following.marks = marks
        following.news.connect(self.news.append)
        if start:
            following.start()
            self.settle()
        return following

    # -- what happened before -----------------------------------------------------

    def submit(self, account: TestAccount, *, d: str = "ein-jahr", stored: bool = True,
               listed: bool = True, send: bool = True, **more):
        """A submission of ``account``: sent to the server (``send``), its
        entry on the list (``listed``), marked stored or not."""
        prepared = submission_for(sk("writer") if account.writer == pk("writer")
                                  else account.key.secret, d=d, **more)
        if send:
            outcome = []
            account.api.submit(prepared.body, outcome.append, outcome.append)
            self.settle()
            assert type(outcome[0]).__name__ == "Confirmed", outcome
        entry = entry_of(prepared, d=d, stored=stored)
        if listed:
            done = []
            account.submissions.record(entry, done.append)
            self.settle()
            assert done and done[0].stored, done
            entry = done[0].entry
        return prepared, entry

    def message(self, prepared, text: str, *, sender: str = "editor",
                forge_author: Optional[str] = None) -> dict:
        """A message in the submission's room, posted by ``sender``."""
        wrap = room_wrap(prepared, conversation.message_rumor(
            room=scrypto.pubkey_of(prepared.s_sub),
            submission_id=prepared.body["submission_id"], text=text,
            sender=forge_author or pk(sender), created_at=int(self.clock())), sender)
        self.post(prepared, wrap, sender=sender)
        return wrap

    def post(self, prepared, wrap: dict, *, sender: str = "editor",
             kind_hint: str = "message") -> int:
        api = self.editor if sender == "editor" else StandupApi(
            SignAs(sk(sender), clock=self.clock), server=SERVER, nam=self.nam, clock=self.clock)
        outcome = []
        api.send_message(prepared.body["submission_id"], wrap, kind_hint=kind_hint,
                         on_success=outcome.append, on_failure=outcome.append)
        self.settle()
        assert isinstance(outcome[0], int), outcome
        return outcome[0]

    def decide(self, prepared, outcome: str, *, text: str = "", revision: int = 1,
               split=None):
        """The editor's decision; ``split`` the shares of an accept's
        split-proposal (9.4), sent as its split_wrap and named by id."""
        wrap = split_wrap = split_id = None
        if split:
            rumor = conversation.structured_rumor(
                room=scrypto.pubkey_of(prepared.s_sub),
                submission_id=prepared.body["submission_id"], kind="split-proposal",
                content=conversation.split_proposal_content(prepared.body["revision_hash"],
                                                            split),
                sender=pk("editor"), created_at=int(self.clock()))
            split_wrap, split_id = room_wrap(prepared, rumor, "editor"), rumor["id"]
        if text:
            wrap = room_wrap(prepared, conversation.message_rumor(
                room=scrypto.pubkey_of(prepared.s_sub),
                submission_id=prepared.body["submission_id"], text=text, sender=pk("editor"),
                created_at=int(self.clock())), "editor")
        answer = []
        self.editor.decision(prepared.body["submission_id"], outcome=outcome,
                             expected_revision=revision, message_wrap=wrap,
                             split_wrap=split_wrap, split_rumor_id=split_id,
                             on_success=answer.append, on_failure=answer.append)
        self.settle()
        assert hasattr(answer[0], "submission"), answer
        return answer[0].submission

    def take(self, prepared):
        answer = []
        self.editor.responsible(prepared.body["submission_id"], action="take",
                                on_success=answer.append, on_failure=answer.append)
        self.settle()
        return answer[0]

    def record(self, sid: str):
        return self.server.submissions[sid]

    def correct(self, account: "TestAccount", sid: str, content: str, *, media=(),
                note: str = "Zwei Zahlen korrigiert.", revision: int = 2) -> None:
        """The editor's correction (``revision``) of ``account``'s published work."""
        s_sub = bytes.fromhex(account.submissions.entry(sid).s_sub)
        rev = formats.build_revision(
            submission_id=sid, revision=revision, kind="article",
            work={"title": "Ein Jahr mit Lightning im Café", "content": content, "d": "ein-jahr"},
            rights={"confirmed": True, "license": None}, media=list(media),
            change_summary=note, client="MyEditor tests")
        sealed = scrypto.seal_revision(rev, s_sub)
        wraps = scrypto.wrap_submission_key(sid, s_sub, {
            "redaktion": json.loads(KEY_EVENT["content"])["pubkey"], "writer": pk("writer")})
        answer = []
        self.editor.correction(sid, revision=revision, revision_hash=sealed.revision_hash,
                               envelope=sealed.envelope_b64,
                               envelope_sha256=sealed.envelope_sha256, wraps=wraps, blobs=[],
                               on_success=answer.append, on_failure=answer.append)
        self.settle()
        assert hasattr(answer[0], "submission"), answer


def entry_of(prepared, *, d: str, stored: bool = True) -> SubmissionEntry:
    body = prepared.body
    return SubmissionEntry(
        sid=body["submission_id"], publication=body["publication"], server=SERVER,
        role="writer", s_sub=prepared.s_sub.hex(), status_key=prepared.status_key,
        title=body["title"], kind=body["kind"], d=d, round=body["round"],
        revision=body["revision"], revision_hash=body["revision_hash"],
        envelope_sha256=body["envelope_sha256"],
        wraps_sha256=scrypto.wraps_sha256(body["wraps"]), stored=stored, updated_at=int(NOW))


def room_wrap(prepared, rumor: dict, sender: str) -> dict:
    """``rumor`` sealed by ``sender`` and wrapped to the submission's
    room; a rumor that names another author keeps that name inside (a
    forged sender, which a reader must not show)."""
    room_sk = prepared.s_sub
    room = scrypto.pubkey_of(room_sk)
    secret = sk(sender)
    seal = conversation.seal_unsigned(dict(rumor, pubkey=pk(sender)), room_sk, now=NOW)
    if rumor["pubkey"] != pk(sender):
        import json
        seal["content"] = crypto.encrypt(json.dumps(rumor), crypto.conversation_key(
            secret, bytes.fromhex(room)))
    return conversation.gift_wrap(events.sign_event(seal, secret), room, now=NOW)


__all__ = ["ADDRESS", "AccountSigner", "FollowWorld", "Later", "TestAccount", "entry_of",
           "room_wrap"]
