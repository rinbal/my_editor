# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Submitting, in the protocol's order (nostr/standup/submitting.py),
against the fake STANDUP server that checks what the real one checks
(tests/standup_fakes.py), with a fake clock and fake timers.

Pinned here: the order (key, pictures, revision, the entry on the list,
the signature, the request, the receipt); that only a matching receipt
with every file found is "Eingereicht" (plan criterion 2), from the
receipt's handler and nowhere else; that a request that got no answer
is resolved by the status key without a signer prompt; files a receipt
misses uploaded again and the same body sent again; Abbrechen until the
request is on its way; what each refusal says and whether trying again
can help."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Callable, List

import pytest

from nostr import failures
from nostr.list_results import PublishResult, PublishStatus
from nostr.media.assets import asset_key
from nostr.standup import drafting, formats, submitting, trust, views, words
from nostr.standup import crypto as scrypto
from nostr.standup.client import StandupApi
from nostr.standup.drafting import ArticleDraft, Choice, LocalPicture
from nostr.standup.entries import (
    RecordResult, RecordStatus, SubmissionEntry, SubmissionItems, merge_items, record_key,
    record_of, records_of, same_keys, shown_for, stored_elsewhere, tombstone_for,
)
from nostr.standup.errors import Code, StandupError
from nostr.standup.ports import Reread
from nostr.standup.submitting import Held, MessageStatus, Phase, Ports, Step, Submitting
from nostr.standup.watcher import resolve_unconfirmed
from tests.membership_fakes import FakeClock
from tests.standup_fakes import (
    ADDRESS, DROP_AFTER, DROP_BEFORE, KEY_EVENT, LOST, SERVER, FakeStandupServer, Fault, SignAs,
    StandupNam, submission_for,
)
from tests.standup_vectors import NOW, pk, sk

PICTURE = b"\xff\xd8\xff a photo of the cafe"
PICTURE_KEY = asset_key(formats.sha256_hex(PICTURE))
MEDIA = "https://media.example"


class Later:
    """Timers by hand: what is due runs when the test lets time pass."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waiting: List[tuple] = []
        self.delays: List[int] = []

    def __call__(self, ms: int, callback: Callable[[], None]) -> None:
        self.delays.append(ms)
        self.waiting.append((self.clock() + ms / 1000, callback))

    def run_due(self) -> bool:
        due = [w for w in self.waiting if w[0] <= self.clock()]
        self.waiting = [w for w in self.waiting if w[0] > self.clock()]
        for _when, callback in due:
            callback()
        return bool(due)


class Uploads:
    """The writer's media server: what is uploaded is found by the STANDUP
    server's HEAD request, unless the test loses it; ``hold`` keeps an
    upload going until the test finishes it."""

    def __init__(self, server: FakeStandupServer) -> None:
        self.server = server
        self.calls: List[dict] = []
        self.hold = False
        self.lose = 0
        self.fail = ""

    def __call__(self, pictures, on_progress, on_done, on_failure):
        call = {"pictures": list(pictures), "stopped": False, "on_progress": on_progress,
                "on_done": on_done, "on_failure": on_failure}
        self.calls.append(call)
        if not self.hold:
            self.finish(call)
        return lambda: call.update(stopped=True)

    def finish(self, call=None) -> None:
        call = call or self.calls[-1]
        if self.fail:
            call["on_failure"](self.fail)
            return
        call["on_progress"](0.5)
        for picture in call["pictures"]:
            if self.lose:
                self.lose -= 1
            else:
                self.server.blob_servers.everywhere.add(picture.sha256)
        call["on_done"]({p.sha256: (MEDIA,) for p in call["pictures"]})


class SubmissionsList:
    """The writer's list: records and answers STORED on a later turn,
    unless the test sets another outcome. Like the real one it holds
    records merged by identity (entries.merge_items, 13 at 0456312) and
    refuses keys no record of a known sid names."""

    def __init__(self, later: Later, log: List[str]) -> None:
        self.later = later
        self.log = log
        self.entries = []
        self.forgotten = []
        self.marked = []
        self.items = SubmissionItems()
        self.outcomes: List[RecordResult] = []
        # What another device wrote: found by the next read (reload),
        # merged as the real list merges (merge_items).
        self.arriving = []
        self.unreadable = ""                # a read again that cannot tell

    @property
    def on_list(self):
        """What each sid shows (entries.shown_for)."""
        return {item.sid: shown_for(self.items, item.sid) for item in self.items.items
                if shown_for(self.items, item.sid) is not None}

    def held(self, sid):
        return shown_for(self.items, sid)

    def put(self, *items) -> None:
        self.items = merge_items(self.items, SubmissionItems(tuple(items)))

    def mark_stored(self, entry) -> None:
        # SubmissionsList.mark_stored: the record held with these keys.
        self.marked.append(entry)
        self.log.append("stored")
        held = record_of(self.items, entry.sid, record_key(entry))
        if held is not None and same_keys(held, entry):
            self.put(replace(held, stored=True))

    def reload(self, entry, on_done) -> None:
        # StandupWriter._reread: the list read again, then whether that
        # device's stored entry is here (entries.stored_elsewhere).
        self.log.append("reload")
        if self.unreadable:
            self.later(0, lambda: on_done(Reread(False, self.unreadable)))
            return
        self.put(*self.arriving)
        self.arriving = []
        arrived = stored_elsewhere(self.held(entry.sid), entry)
        self.later(0, lambda: on_done(Reread(arrived)))

    def forget(self, entry) -> None:
        self.forgotten.append(entry)
        self.put(tombstone_for(entry, entry.updated_at + 1))
        self.log.append("forget")

    def record(self, entry, on_done) -> None:
        self.entries.append(entry)
        self.log.append("record")
        known = records_of(self.items, entry.sid)
        if known and record_of(self.items, entry.sid, record_key(entry)) is None:
            result = RecordResult(RecordStatus.NOT_STORED, entry,
                                  PublishResult(PublishStatus.FAILED, reason="other secrets"))
        else:
            result = (self.outcomes.pop(0) if self.outcomes else
                      RecordResult(RecordStatus.STORED, entry, PublishResult(PublishStatus.SENT)))
        if result.status is RecordStatus.STORED:
            self.put(entry)
        self.later(0, lambda: on_done(result))


class HeldSigner(SignAs):
    """A signer app the person has not answered yet: ``release()``."""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self.held = []

    def __call__(self, unsigned, on_success, on_failure) -> None:
        self.held.append((unsigned, on_success, on_failure))

    def release(self) -> None:
        unsigned, on_success, on_failure = self.held.pop(0)
        SignAs.__call__(self, unsigned, on_success, on_failure)


class World:
    def __init__(self, *, signer=None, message: str = "", pictures=True, round_: int = 1,
                 authors=()) -> None:
        self.clock = FakeClock(NOW)
        self.server = FakeStandupServer(self.clock)
        self.nam = StandupNam(responder=self.server)
        self.signer = signer or SignAs(sk("writer"), clock=self.clock)
        self.api = StandupApi(self.signer, server=SERVER, nam=self.nam, clock=self.clock)
        self.later = Later(self.clock)
        self.log: List[str] = []
        self.uploads = Uploads(self.server)
        self.list = SubmissionsList(self.later, self.log)
        self.key_refusal = None
        self.messages: List[tuple] = []
        self.seen: List[submitting.Progress] = []
        draft = ArticleDraft(
            d="ein-jahr-mit-lightning", title="Ein Jahr mit Lightning im Café",
            summary="Wie ein Café in Fürth Bitcoin annimmt.",
            content=f"Seit einem Jahr.\n\n![Das Café]({PICTURE_KEY})\n" if pictures else "Text.",
            tags=("bitcoin",), image=PICTURE_KEY if pictures else "", authors=tuple(authors),
            pictures=(LocalPicture(key=PICTURE_KEY, name="cafe.jpg", mime="image/jpeg",
                                   alt="Das Café", data=PICTURE),) if pictures else ())
        self.choice = Choice(publication=ADDRESS, name="Einundzwanzig Magazin", signer="writer",
                             rights=True, message=message)
        self.submitting = Submitting(draft, self.choice, writer=pk("writer"), round_=round_,
                                     ports=self.ports(), on_change=self.seen.append)

    def ports(self) -> Ports:
        def confirm_key(on_key, on_refused):
            if self.key_refusal is not None:
                on_refused(self.key_refusal)
            else:
                on_key(trust.verify_key_event(KEY_EVENT, ADDRESS))

        def submit(body, on_sending, on_success, on_failure):
            self.log.append("submit")
            call = self.api.submit(body, on_success, on_failure, on_sending=on_sending)
            return lambda: self.api.drop(call)

        def lookup(sealed, on_done, on_failure):
            self.log.append("lookup")
            resolve_unconfirmed(self.api, sealed.entry, on_done, on_failure, body=sealed.body,
                                clock=self.clock)

        def send_message(sealed, text, on_signed, on_sent, on_failure):
            self.messages.append((sealed.submission_id, text))
            on_signed()
            on_sent()

        return Ports(confirm_key=confirm_key, upload=self.uploads, held=self.list.held,
                     record=self.list.record, mark_stored=self.list.mark_stored,
                     forget=self.list.forget, submit=submit, lookup=lookup,
                     send_message=send_message, reload=self.list.reload,
                     later=self.later, clock=self.clock)

    def settle(self, *, seconds: float = 0.0) -> None:
        """Everything due, and what it starts, until nothing moves."""
        if seconds:
            self.clock.advance(seconds)
        for _ in range(100):
            ran = self.later.run_due()
            self.nam.settle()
            if not ran and not any(w[0] <= self.clock() for w in self.later.waiting):
                return
        raise AssertionError("submitting did not settle")

    def go(self) -> submitting.Progress:
        self.submitting.start()
        self.settle()
        return self.submitting.progress

    @property
    def steps(self) -> List[Step]:
        out: List[Step] = []
        for progress in self.seen:
            if not out or out[-1] is not progress.step:
                out.append(progress.step)
        return out

    def posted(self) -> List[dict]:
        return [json.loads(body) for verb, path, body, _h in self.server.requests
                if verb == "POST" and path == "/api/submissions"]

    def nip98(self) -> List[dict]:
        return [r for r in self.signer.requests if r["kind"] == 27235]

    def on_server(self, round_: int, state: str) -> SubmissionEntry:
        """A submission of this work in ``round_`` that the server holds
        in ``state``, sent from elsewhere (the list here knows nothing);
        the entry that device keeps, stored (13 at 72d5ddc)."""
        prepared = submission_for(sk("writer"), round_=round_)
        self.api.submit(prepared.body, lambda _r: None, lambda _e: None)
        self.nam.settle()
        body = prepared.body
        self.server.submissions[body["submission_id"]].state = state
        return SubmissionEntry(
            sid=body["submission_id"], publication=body["publication"], server=SERVER,
            role="writer", s_sub=prepared.s_sub.hex(), status_key=prepared.status_key,
            title=body["title"], kind="article", d="ein-jahr-mit-lightning", round=round_,
            revision=1, revision_hash=body["revision_hash"],
            envelope_sha256=body["envelope_sha256"],
            wraps_sha256=scrypto.wraps_sha256(body["wraps"]), stored=True,
            updated_at=int(NOW) - 60)


def test_an_article_goes_out_in_the_protocols_order_and_counts_only_with_its_receipt():
    world = World()
    progress = world.go()
    assert world.steps == [Step.KEY, Step.ENCRYPTING, Step.UPLOADING, Step.RECORDING,
                           Step.SIGNING, Step.SENDING, Step.SUBMITTED]
    assert progress.phase is Phase.DONE and progress.receipt is not None
    # The entry was on the list before the request was made (12.4), and is
    # marked stored once the receipt is held (13 at 72d5ddc).
    assert world.log == ["record", "submit", "stored"]
    entry = world.list.entries[0]
    assert world.list.on_list[entry.sid] == replace(entry, stored=True)
    body = world.posted()[0]
    assert entry.sid == body["submission_id"] and entry.revision_hash == body["revision_hash"]
    assert formats.status_key_hash(entry.status_key) == body["status_key_hash"]
    assert progress.receipt.revision_hash == body["revision_hash"]
    # The picture travels encrypted, named by its cipher hash.
    sha = world.uploads.calls[0]["pictures"][0].sha256
    assert body["blobs"] == [{"sha256": sha, "size": body["blobs"][0]["size"],
                              "servers": [MEDIA]}]
    assert PICTURE not in json.dumps(body).encode()


def test_the_revision_names_the_pictures_by_their_encrypted_files():
    world = World()
    world.go()
    sealed = world.submitting.sealed
    # Opened the way the Redaktion opens it (a test may reach in).
    revision = scrypto._open_revision(
        sealed.body["envelope"], sealed.s_sub, submission_id=sealed.submission_id, revision=1,
        revision_hash=sealed.body["revision_hash"])
    ref = formats.media_ref(world.uploads.calls[0]["pictures"][0].sha256)
    assert revision["work"]["image"] == ref
    assert f"![Das Café]({ref})" in revision["work"]["content"]
    assert PICTURE_KEY not in revision["work"]["content"]
    assert revision["media"][0]["servers"] == [MEDIA]


def test_upload_progress_moves_the_bar_and_stays_between_nothing_and_all():
    world = World()
    world.uploads.hold = True
    world.submitting.start()
    world.settle()
    assert world.submitting.progress.step is Step.UPLOADING
    world.uploads.calls[0]["on_progress"](0.4)
    world.uploads.calls[0]["on_progress"](7)
    assert [p.uploaded for p in world.seen if p.step is Step.UPLOADING][-2:] == [0.4, 1.0]


@pytest.mark.parametrize("step", [Step.KEY, Step.UPLOADING, Step.RECORDING, Step.SIGNING])
def test_abbrechen_until_the_request_is_on_its_way_sends_nothing(step):
    signer = HeldSigner(sk("writer"))
    world = World(signer=signer)
    world.uploads.hold = True
    if step is Step.KEY:        # the relays have not answered yet
        world.submitting._ports = replace(world.submitting._ports,
                                          confirm_key=lambda on_key, on_refused: None)
    world.submitting.start()
    world.settle()
    if step in (Step.RECORDING, Step.SIGNING):
        world.uploads.finish()
        if step is Step.SIGNING:
            world.settle()
    assert world.submitting.progress.step is step
    assert world.submitting.progress.cancellable
    world.submitting.cancel()
    assert world.submitting.progress.step is Step.CANCELLED
    if signer.held:
        signer.release()        # the person said yes on the phone, too late
    world.settle(seconds=60)
    assert world.posted() == []
    assert world.submitting.progress.step is Step.CANCELLED
    if step is Step.UPLOADING:
        assert world.uploads.calls[0]["stopped"]
    # An entry recorded for it is taken back (RG-M2b 1); none was before.
    recorded = step in (Step.RECORDING, Step.SIGNING)
    assert world.list.forgotten == (world.list.entries[:1] if recorded else [])


def test_once_the_request_is_on_its_way_abbrechen_changes_nothing():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    world.submitting.start()
    for _ in range(10):
        world.later.run_due()
        if world.submitting.progress.step is Step.SENDING:
            break
        world.nam.settle()
    assert world.submitting.progress.step in (Step.SENDING, Step.CHECKING)
    assert not world.submitting.progress.cancellable
    world.submitting.cancel()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED


def test_no_answer_after_sending_is_resolved_by_the_status_key_without_a_prompt():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    progress = world.go()
    assert Step.CHECKING in world.steps
    assert progress.step is Step.SUBMITTED
    # The lookup's receipt is held too: the entry is marked stored.
    assert world.log == ["record", "submit", "lookup", "stored"]
    assert len(world.nip98()) == 1              # the lookup needed no signature
    assert len(world.posted()) == 1


def test_a_lookup_that_cannot_be_asked_keeps_checking_and_never_reads_as_submitted():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    for _ in range(8):
        world.server.inject("lookup", DROP_BEFORE)
    world.go()
    for _ in range(8):
        world.settle(seconds=300)
    assert world.submitting.progress.step is Step.SUBMITTED   # the ninth answered
    lookups = [d for d in world.later.delays if d]
    assert lookups[:5] == list(submitting.LOOKUP_DELAYS_MS[1:6])
    assert Step.FAILED not in world.steps


def test_while_nobody_answers_it_stays_unconfirmed():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    for _ in range(50):
        world.server.inject("lookup", DROP_BEFORE)
    world.go()
    for _ in range(10):
        world.settle(seconds=300)
    assert world.submitting.progress.step is Step.CHECKING
    assert world.submitting.progress.phase is Phase.UNCONFIRMED
    assert Step.SUBMITTED not in world.steps


def _tamper(name, value):
    def change(receipt):
        return dict(receipt, **{name: value})
    return change


def test_a_receipt_that_does_not_match_is_checked_and_confirmed_only_by_the_lookup():
    world = World()
    tamper, answers = _tamper("envelope_sha256", "00" * 32), []

    def first_answer_only(receipt):
        answers.append(receipt)
        return tamper(receipt) if len(answers) == 1 else receipt

    world.server.tamper_receipt = first_answer_only
    progress = world.go()
    # The answer did not match: never "Eingereicht" from it. The lookup's
    # receipt matches what was sent in every field, and that confirms.
    assert world.steps[-2:] == [Step.CHECKING, Step.SUBMITTED]
    assert progress.step is Step.SUBMITTED


@pytest.mark.parametrize("field, value", [
    ("envelope_sha256", "00" * 32),
    ("wraps_sha256", {"writer": "00" * 32}),
    ("blobs_present", []),
])
def test_a_lookup_that_finds_another_envelope_or_other_wraps_is_never_submitted(field, value):
    # RG-M2b 11: the lookup compared revision and hash only, so a server
    # holding other wraps of the same revision read as "Eingereicht".
    world = World()
    world.server.tamper_receipt = _tamper(field, value)
    progress = world.go()
    assert Step.CHECKING in world.steps and Step.SUBMITTED not in world.steps
    # 72d688b: this revision is stored, not as it was sent; the words say
    # so, and never that nothing is submitted.
    assert progress.step is Step.FAILED and progress.failure.sentence == words.STORED_OTHER
    assert progress.failure.title == words.VERSION_NOT_SUBMITTED


def test_a_receipt_for_another_version_is_never_submitted():
    world = World()
    world.server.tamper_receipt = _tamper("revision_hash", "ab" * 32)
    progress = world.go()
    assert Step.SUBMITTED not in world.steps
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == words.ANOTHER_VERSION
    assert progress.failure.retry is None


def test_a_file_the_server_misses_is_uploaded_again_and_the_same_body_sent_again():
    world = World()
    world.uploads.lose = 1
    progress = world.go()
    assert world.steps == [Step.KEY, Step.ENCRYPTING, Step.UPLOADING, Step.RECORDING,
                           Step.SIGNING, Step.SENDING, Step.REUPLOADING, Step.SIGNING,
                           Step.SENDING, Step.SUBMITTED]
    first, second = world.posted()
    assert first == second                       # the same body (12.4)
    assert len(world.nip98()) == 2               # with a new signature
    assert progress.step is Step.SUBMITTED and not progress.receipt.blobs_missing
    # An exact repeat of the stored revision 1 is its receipt, never
    # stale_revision (contract 5ddb799): no round is chosen again.
    assert world.log.count("round") == 0


def test_a_file_that_stays_missing_fails_with_a_retry():
    world = World()
    world.uploads.lose = 10
    progress = world.go()
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == words.PICTURE_MISSING
    assert progress.failure.retry is Step.REUPLOADING
    assert len(world.uploads.calls) == 1 + submitting.MAX_REUPLOADS
    world.uploads.lose = 0
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED


def test_once_the_server_stored_it_nothing_is_cancelled_or_called_not_submitted():
    # RG-M2b 3: after a receipt with a file missing the run goes back to
    # the signer for the same body; it is not cancellable, and a failure
    # after it says what the server has.
    world = World(signer=HeldSigner(sk("writer")))
    world.uploads.lose = 1
    world.submitting.start()
    world.settle()
    world.signer.release()
    world.settle()
    progress = world.submitting.progress
    assert progress.step is Step.SIGNING and len(world.posted()) == 1
    assert not progress.cancellable
    world.submitting.cancel()
    assert world.submitting.progress.step is Step.SIGNING
    world.signer.held[0][2]("declined")             # the person says no on the phone
    world.settle()
    failure = world.submitting.progress.failure
    assert failure is not None and failure.title == words.PICTURE_NOT_IN
    assert world.list.forgotten == []               # its entry stays: the server has it


# -- what the server holds (RG-M2r 2, lesson 32) ----------------------------------

def test_every_held_fact_has_its_words_and_its_place():
    assert set(submitting.HELD_WORDS) == set(Held)
    assert sorted(submitting.HELD_UNKNOWN_FIRST, key=lambda h: h.value) \
        == sorted(Held, key=lambda h: h.value)


@pytest.mark.parametrize("set_up, held", [
    (lambda w: setattr(w.server.publication(), "paused", True), Held.NOTHING),
    (lambda w: w.server.inject("submit", Fault(429, "rate_limited", retry_after=60)),
     Held.NOTHING),
    (lambda w: setattr(w.uploads, "lose", 10), Held.STORED),
    (lambda w: w.server.inject("submit", Fault(409, "stale_revision")), Held.OTHER),
    (lambda w: setattr(w.server, "tamper_receipt", _tamper("revision_hash", "ab" * 32)),
     Held.OTHER),
    (lambda w: setattr(w.server, "tamper_receipt", _tamper("revision", 2)), Held.OTHER),
    (lambda w: setattr(w.server, "tamper_receipt", _tamper("envelope_sha256", "00" * 32)),
     Held.OTHER),
], ids=["refused", "rate limited", "stored, a picture missing", "stale after sending",
        "another version", "a later version", "not as sent"])
def test_a_failure_says_what_the_server_holds(set_up, held):
    # RG-M2r 2: a lookup that finds another version, or a stale refusal
    # after sending, said "Nothing is submitted": the server holds one. A
    # stale revision 1 is another device's round (72d5ddc): what it says
    # of this device's list comes in place of the table's line.
    world = World()
    set_up(world)
    progress = world.go()
    assert progress.step is Step.FAILED and progress.held is held
    elsewhere = progress.failure.retry is Step.RELOADING
    assert progress.failure.title == (words.SUBMITTED_ELSEWHERE if elsewhere
                                      else submitting.HELD_WORDS[held].failed)
    if held is not Held.NOTHING:
        assert progress.failure.title != words.NOT_SUBMITTED


def test_a_receipt_with_a_picture_missing_marks_the_entry_stored_once():
    # The receipt with blobs missing says the server stores this sid with
    # this status key (13 at 72d5ddc): marked then, not again per resend.
    world = World()
    world.uploads.lose = 1
    progress = world.go()
    assert progress.step is Step.SUBMITTED
    assert world.log == ["record", "submit", "stored", "submit"]
    assert [m.stored for m in world.list.marked] == [True]
    assert world.list.on_list[world.list.entries[0].sid].stored


def test_a_run_never_marks_what_it_holds_no_receipt_for():
    world = World()
    world.server.inject("submit", Fault(429, "rate_limited", retry_after=60))
    world.go()
    assert world.list.marked == []
    assert not world.list.on_list[world.list.entries[0].sid].stored


def test_what_the_server_holds_follows_each_answer():
    # Nothing until the request goes out; maybe while it has no answer;
    # stored once a receipt says so, also while the same body goes again
    # for a picture it missed; nothing again when the lookup finds none.
    world = World()
    world.uploads.lose = 1
    progress = world.go()
    held = {}
    for seen in world.seen:
        held.setdefault(seen.step, set()).add(seen.held)
    assert held[Step.RECORDING] == {Held.NOTHING}
    assert held[Step.SENDING] == {Held.MAYBE, Held.STORED}
    assert held[Step.REUPLOADING] == {Held.STORED}
    assert progress.step is Step.SUBMITTED and progress.held is Held.STORED

    world = World()
    world.server.inject("submit", DROP_BEFORE)
    world.go()
    # (DROP_BEFORE never reaches the server: nothing.)
    assert world.submitting.progress.held is Held.NOTHING
    world = World()
    world.server.inject("submit", LOST)
    world.submitting.start()
    world.settle()
    progress = world.submitting.progress
    assert progress.step is Step.FAILED and progress.failure.sentence == words.NOT_RECEIVED
    assert {s.held for s in world.seen if s.step is Step.CHECKING} == {Held.MAYBE}
    assert progress.held is Held.NOTHING


def test_an_answer_to_an_earlier_request_is_ignored():
    world = World()
    world.uploads.fail = "The server is full."
    world.go()
    # What the servers answered: behind Details only (lessons 36).
    failure = world.submitting.progress.failure
    assert (failure.details, failure.said) == ("The server is full.", "")
    first = world.uploads.calls[0]
    world.uploads.fail, world.uploads.hold = "", True
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.UPLOADING
    first["on_done"]({p.sha256: (MEDIA,) for p in first["pictures"]})   # late, and twice
    world.settle()
    assert world.submitting.progress.step is Step.UPLOADING
    world.uploads.finish()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED


def test_a_revision_that_cannot_be_built_here_never_reads_as_the_servers_refusal():
    # RG-M2b 11: nothing went out (lesson 8), so not "the server didn't take it".
    world = World(authors=(("ff" * 32, ""),))
    progress = world.go()
    assert progress.step is Step.FAILED and progress.failure.sentence == words.NOT_BUILT
    assert progress.failure.title == words.NOT_SUBMITTED and not progress.sent
    assert "submit" not in world.server.handled


def test_a_request_the_server_never_got_is_said_not_received_and_can_be_sent_again():
    # RG-M2b 11: the lookup answered, so it is not "no connection".
    world = World()
    world.server.inject("submit", LOST)
    progress = world.go()
    assert Step.CHECKING in world.steps and progress.step is Step.FAILED
    assert progress.failure.sentence == words.NOT_RECEIVED
    assert progress.failure.title == words.NOT_SUBMITTED
    assert progress.failure.retry is Step.SIGNING
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED


def test_a_connection_that_failed_before_sending_is_not_sent_and_can_be_sent_again():
    world = World()
    world.server.inject("submit", DROP_BEFORE)
    progress = world.go()
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == failures.CHECK_CONNECTION
    assert progress.failure.title == words.NOT_SUBMITTED
    assert progress.failure.retry is Step.SIGNING
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED
    assert len(world.list.entries) == 1          # recorded once, sent twice
    first, second = world.posted()
    assert first == second


@pytest.mark.parametrize("set_up, code, retry", [
    (lambda w: setattr(w.server.publication(), "paused", True), "paused", None),
    (lambda w: w.server.publication().blocked.add(pk("writer")), "blocked", None),
    (lambda w: w.server.inject("submit", Fault(422, "key_event_invalid")), "key_event_invalid",
     Step.KEY),
    (lambda w: w.server.inject("submit", Fault(409, "epoch")), "epoch", Step.KEY),
    (lambda w: w.server.inject("submit", Fault(409, "no_decider")), "no_decider", None),
    (lambda w: w.server.inject("submit", Fault(429, "rate_limited", retry_after=600)),
     "rate_limited", Step.SIGNING),
    (lambda w: w.server.inject("submit", Fault(413, "too_large")), "too_large", None),
])
def test_each_refusal_says_what_it_is_and_whether_trying_again_helps(set_up, code, retry):
    world = World()
    set_up(world)
    progress = world.go()
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == ({"key_event_invalid": words.KEY_NOT_CONFIRMED_AGAIN,
                                          "epoch": words.KEY_NOT_CONFIRMED_AGAIN}.get(code)
                                         or words.refusal(code))
    assert progress.failure.retry is retry
    assert Step.SUBMITTED not in world.steps


# -- a limit's wait (RG-M2r 7, contract 15) -----------------------------------------

@pytest.mark.parametrize("wait", [600, 7 * 3600, None],
                         ids=["an hour's", "the day's", "no Retry-After"])
def test_a_limit_says_which_it_is_and_trying_again_waits_for_it(wait):
    # The sentence says when trying again helps, from retry_at (the
    # handoff's phase_words, lessons 36).
    world = World(pictures=False)
    world.server.inject("submit", Fault(429, "rate_limited", retry_after=wait))
    progress = world.go()
    failure = progress.failure
    assert failure.sentence == words.refusal("rate_limited") and failure.retry is Step.SIGNING
    assert failure.retry_at == (None if wait is None else NOW + wait)
    prompts, posted = len(world.nip98()), len(world.posted())
    if wait is not None:
        world.settle(seconds=wait - 1)
        world.submitting.retry()            # before the wait: no prompt, no request
        world.settle()
        assert (len(world.nip98()), len(world.posted())) == (prompts, posted)
        world.settle(seconds=1)
    world.submitting.retry()
    world.settle()
    assert len(world.posted()) == posted + 1
    assert world.submitting.progress.step is Step.SUBMITTED


def test_a_lookup_refused_for_its_limit_asks_again_only_after_the_wait():
    world = World(pictures=False)
    world.server.inject("submit", DROP_AFTER)
    world.server.inject("lookup", Fault(429, "rate_limited", retry_after=120))
    world.submitting.start()
    world.settle()
    asked = world.log.count("lookup")
    world.settle(seconds=119)
    assert world.log.count("lookup") == asked
    world.settle(seconds=1)
    assert world.log.count("lookup") == asked + 1
    assert world.submitting.progress.step is Step.SUBMITTED


def test_a_key_that_is_not_confirmed_stops_everything_before_it_starts():
    world = World()
    world.key_refusal = trust.KeyNotConfirmed("an older epoch")
    progress = world.go()
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == words.KEY_NOT_CONFIRMED_AGAIN
    assert world.uploads.calls == [] and world.list.entries == [] and world.posted() == []


def test_an_entry_the_list_does_not_hold_is_never_sent():
    world = World()
    world.list.outcomes.append(RecordResult(
        RecordStatus.SUPERSEDED, None, PublishResult(PublishStatus.SENT)))
    progress = world.go()
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == words.LIST_CHANGED_ELSEWHERE
    assert progress.failure.retry is None
    assert world.posted() == []


@pytest.mark.parametrize("status, said", [
    (PublishStatus.BLOCKED, "Your list of submissions couldn't be read."),
    (PublishStatus.UNREADABLE, "Your list of submissions couldn't be read."),
    (PublishStatus.NO_ANSWER, ""),
])
def test_a_list_that_could_not_be_written_says_so_and_offers_to_try_again(status, said):
    world = World()
    world.list.outcomes.append(RecordResult(
        RecordStatus.NOT_STORED, None, PublishResult(status, reason=said)))
    progress = world.go()
    assert progress.step is Step.FAILED and world.posted() == []
    assert progress.failure.sentence == words.LIST_NOT_SAVED
    assert progress.failure.said == said
    assert progress.failure.retry is Step.RECORDING
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED


def test_a_secret_key_in_the_text_is_refused_before_anything_is_recorded():
    world = World(pictures=False)
    world.submitting._draft = ArticleDraft(
        d="x", title="Titel", summary="",
        content="nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5")
    progress = world.go()
    assert progress.step is Step.FAILED
    assert progress.failure.sentence == formats.SECRET_KEY_IN_SUBMISSION
    assert world.list.entries == [] and world.posted() == []


def test_the_message_goes_out_only_after_the_receipt():
    world = World(message="  Warum der Text zu euch passt.  ")
    world.server.inject("submit", DROP_AFTER)
    progress = world.go()
    statuses = [(p.step, p.message) for p in world.seen if p.message is not MessageStatus.NONE]
    # The signer app is asked first, and the words say so (RG-M2b 11).
    assert statuses[:2] == [(Step.SUBMITTED, MessageStatus.SIGNING),
                            (Step.SUBMITTED, MessageStatus.SENDING)]
    assert progress.message is MessageStatus.SENT
    assert world.messages == [(world.submitting.sealed.submission_id,
                               "Warum der Text zu euch passt.")]


def test_a_message_that_failed_can_be_sent_again_and_the_submission_stays_submitted():
    world = World(message="Hallo")
    answers = [False, True]

    def send_message(sealed, text, on_signed, on_sent, on_failure):
        (on_sent if answers.pop(0) else on_failure)()

    world.submitting._ports = replace(world.submitting._ports, send_message=send_message)
    progress = world.go()
    assert progress.step is Step.SUBMITTED and progress.message is MessageStatus.FAILED
    world.submitting.retry_message()
    assert world.submitting.progress.message is MessageStatus.SENT


def test_a_message_refused_for_its_limit_is_sent_again_only_after_the_wait():
    world = World(message="Hallo")
    answers = [90, None]
    sent = []

    def send_message(sealed, text, on_signed, on_sent, on_failure):
        sent.append(text)
        wait = answers.pop(0)
        on_failure(wait) if wait else on_sent()

    world.submitting._ports = replace(world.submitting._ports, send_message=send_message)
    progress = world.go()
    assert progress.message is MessageStatus.FAILED and progress.message_retry_at == NOW + 90
    world.submitting.retry_message()                # before the wait: nothing is sealed
    assert sent == ["Hallo"]
    world.settle(seconds=90)
    world.submitting.retry_message()
    assert sent == ["Hallo", "Hallo"]
    progress = world.submitting.progress
    assert progress.message is MessageStatus.SENT and progress.message_retry_at is None


def test_eingereicht_comes_only_from_the_receipts_handler():
    # The table is the one place a step follows another: only a
    # Confirmed (the server's answer, or the lookup's) leads there.
    leads_there = {key for key, name in Submitting._TABLE.items() if name == "_confirmed"}
    assert leads_there == {(Step.SENDING, views.Confirmed), (Step.CHECKING, views.Confirmed)}
    world = World()
    calls = []
    original = Submitting._confirmed

    def watched(self, confirmed):
        calls.append(confirmed)
        original(self, confirmed)

    Submitting._confirmed = watched
    try:
        world.go()
    finally:
        Submitting._confirmed = original
    first = next(i for i, p in enumerate(world.seen) if p.step is Step.SUBMITTED)
    assert len(calls) == 1 and world.seen[first].receipt is calls[0].receipt


@pytest.mark.parametrize("error, handler", [
    (StandupError(Code.OFFLINE, maybe_stored=True), "_check"),
    (StandupError(Code.CONFLICT, detail={"round": 2, "open": False}), "_other_round"),
    (StandupError(Code.CONFLICT, detail={"round": 2, "open": True}), "_round_open"),
    (StandupError(Code.CONFLICT, detail={"round": 2}), "_refused"),
    (StandupError(Code.CONFLICT, detail={"round": "2", "open": False}), "_refused"),
    (StandupError(Code.STALE_REVISION, detail={"round": 2, "open": False}), "_stale"),
    (StandupError(Code.STALE_REVISION, detail={"revision": 1}), "_stale"),
    (StandupError(Code.OFFLINE), "_refused"),
], ids=["maybe stored", "closed round", "open round", "no open", "round as text", "not a conflict",
        "stale", "offline"])
def test_what_a_refusal_leads_to_is_read_from_the_table(error, handler):
    # Lesson 10: no handler decides what follows a refusal; each kind is an
    # event of its own, and the table names its handler, before and once
    # the request went out.
    from nostr.standup.submitting import _refusal
    for step in (Step.SIGNING, Step.SENDING):
        assert Submitting._TABLE[(step, type(_refusal(error)))] == handler


def test_an_upload_failure_is_tried_again_from_where_it_happened_by_the_table():
    from nostr.standup.submitting import _UploadFailed
    assert Submitting._TABLE[(Step.UPLOADING, _UploadFailed)] == "_upload_failed"
    assert Submitting._TABLE[(Step.REUPLOADING, _UploadFailed)] == "_reupload_failed"


def test_after_stop_nothing_more_is_asked_or_said():
    world = World()
    world.server.inject("submit", DROP_AFTER)
    world.server.inject("lookup", DROP_BEFORE)
    world.submitting.start()
    for _ in range(6):
        world.later.run_due()
        world.nam.settle()
    world.submitting.stop()
    said = len(world.seen)
    world.settle(seconds=600)
    assert len(world.seen) == said


# -- the round the server expects, and one set of secrets per sid ------------------

def test_built_again_after_a_key_change_it_keeps_the_secrets_of_its_sid():
    # Contract 2: a new body for a sid the list names keeps its keys;
    # new ones would be refused by the list (RG-M2b 1).
    world = World(pictures=False)
    world.server.inject("submit", Fault(409, "epoch"))
    progress = world.go()
    assert progress.failure.retry is Step.KEY
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED
    first, second = world.list.entries
    assert (first.s_sub, first.status_key) == (second.s_sub, second.status_key)
    assert world.posted()[0]["p_sub"] == world.posted()[1]["p_sub"]


def test_the_round_the_server_expects_is_built_once_and_the_refused_entry_goes():
    # The list here said round 3; the server's latest round of the work
    # is 1, declined: 409 conflict {"round": 1, "open": false}.
    world = World(pictures=False, round_=3)
    world.on_server(1, "declined")
    progress = world.go()
    assert progress.step is Step.SUBMITTED
    refused, sent = world.posted()[-2:]
    assert (refused["round"], sent["round"]) == (3, 2)
    assert refused["p_sub"] != sent["p_sub"]        # another sid, its own secrets
    assert [e.round for e in world.list.forgotten] == [3]
    assert world.list.held(sent["submission_id"]).round == 2
    # Nothing of the refused body is held: the rebuilt one starts from
    # nothing (lesson 32), so a stop meanwhile says nothing was sent.
    rebuilt = world.seen[[s.step for s in world.seen].index(Step.SENDING) + 1:]
    assert {s.held for s in rebuilt if s.step is Step.RECORDING} == {Held.NOTHING}


def test_a_round_still_open_there_is_said_and_never_sent_again():
    world = World(pictures=False, round_=2)
    world.on_server(1, "in_review")
    progress = world.go()
    assert progress.step is Step.FAILED and progress.failure.retry is None
    assert progress.failure.sentence == drafting.ALREADY_SUBMITTED
    assert progress.failure.title == words.VERSION_NOT_SUBMITTED and progress.held is Held.OTHER
    assert [body["round"] for body in world.posted()[1:]] == [2]
    assert [e.round for e in world.list.forgotten] == [2]


# -- a revision 1 at a sid the server stores (contract 2 at 72d5ddc) --------------

def _sent_here(world, placed: int) -> List[int]:
    """The rounds of the bodies the run sent, after ``placed`` from elsewhere."""
    return [body["round"] for body in world.posted()[placed:]]


@pytest.mark.parametrize("state", ["submitted", "declined"])
def test_a_revision_1_at_a_sid_stored_elsewhere_builds_nothing_and_waits_for_that_entry(state):
    # Another device of this writer built the same round with its own
    # secrets and the server stored that one; this list does not know it.
    # The fake answers as the backend does (409 stale_revision {"revision":
    # 1}). The run never repeats the body, never builds this round or
    # another, keeps its entry not stored with no tombstone, reads the
    # list again and says so, calmly, with Reload (RG-FMb).
    world = World(pictures=False)
    theirs = world.on_server(1, state)
    placed = len(world.posted())
    progress = world.go()
    assert _sent_here(world, placed) == [1] and world.log.count("reload") == 1
    assert progress.step is Step.FAILED and progress.held is Held.OTHER
    failure = progress.failure
    assert (failure.title, failure.sentence, failure.retry, failure.error) == (
        words.SUBMITTED_ELSEWHERE, words.ELSEWHERE_NOT_HERE_YET, Step.RELOADING, False)
    assert Step.RELOADING in world.steps
    assert {s.held for s in world.seen if s.step is Step.RELOADING} == {Held.OTHER}
    mine = world.list.on_list[theirs.sid]
    assert not mine.stored and not same_keys(mine, theirs)
    assert world.list.forgotten == [] and world.list.marked == []
    # Reload until that device's entry is here: nothing is sent meanwhile.
    world.submitting.retry()
    world.settle()
    assert world.log.count("reload") == 2 and _sent_here(world, placed) == [1]
    assert world.submitting.progress.failure.retry is Step.RELOADING
    world.list.arriving.append(theirs)               # older, but stored: it shows
    world.submitting.retry()
    world.settle()
    progress = world.submitting.progress
    assert (progress.failure.title, progress.failure.sentence, progress.failure.retry,
            progress.failure.error) == (words.SUBMITTED_ELSEWHERE, words.ELSEWHERE_HERE_NOW,
                                        None, False)
    assert world.list.on_list[theirs.sid] == theirs
    assert _sent_here(world, placed) == [1] and world.log.count("submit") == 1
    refused = world.posted()[placed]["revision_hash"]
    assert [body["revision_hash"] for body in world.posted()].count(refused) == 1


def test_a_list_that_cannot_be_read_again_says_so_and_reload_stays():
    world = World(pictures=False)
    world.on_server(1, "submitted")
    world.list.unreadable = "Your list of submissions couldn't be read."
    progress = world.go()
    assert progress.failure.retry is Step.RELOADING
    assert progress.failure.said == world.list.unreadable
    assert progress.failure.sentence == words.ELSEWHERE_NOT_HERE_YET


def test_no_reload_while_reading_the_list_again():
    # One read at a time: Reload is only there once the read answered.
    world = World(pictures=False)
    world.on_server(1, "submitted")
    world.submitting.start()
    for _ in range(100):
        if world.submitting.progress.step is Step.RELOADING:
            break
        world.later.run_due()
        world.nam.settle()
    assert world.submitting.progress.step is Step.RELOADING
    world.submitting.retry()
    world.settle()
    assert world.log.count("reload") == 1


def test_a_stale_revision_after_a_receipt_chooses_no_other_round():
    # The server stored this revision (a receipt, a picture missing); a
    # stale_revision for the same body sent again says another version
    # came since: the work is there, no new round.
    world = World()
    world.uploads.lose = 1
    original = world.submitting._ports.submit
    sent = []

    def second_is_stale(body, on_sending, on_success, on_failure):
        sent.append(body)
        if len(sent) == 2:
            world.server.inject("submit", Fault(409, "stale_revision", detail={"revision": 2}))
        return original(body, on_sending, on_success, on_failure)

    world.submitting._ports = replace(world.submitting._ports, submit=second_is_stale)
    progress = world.go()
    assert progress.step is Step.FAILED and progress.failure.sentence == words.ANOTHER_VERSION
    assert world.log.count("round") == 0 and progress.held is Held.OTHER


@pytest.mark.parametrize("said, rounds, headline", [
    ({"round": 1, "open": False}, [1, 2], words.NOT_TAKEN),     # then it wants round 1
    ({"round": 0, "open": False}, [1], words.NOT_TAKEN),        # the round just refused
    ({"round": 1}, [1], words.refusal("conflict")),             # half an answer: a refusal
    # Contract f171b52: a revision under another status key stores nothing.
    ({"field": "status_key_hash"}, [1], words.refusal("conflict")),
], ids=["twice", "the same round", "half an answer", "another status key"])
def test_the_round_is_built_again_once_never_twice(said, rounds, headline):
    world = World(pictures=False)
    world.server.inject("submit", Fault(409, "conflict", detail=said))
    progress = world.go()
    assert progress.step is Step.FAILED and progress.failure.sentence == headline
    assert progress.failure.retry is None
    assert [body["round"] for body in world.posted()] == rounds


# -- Try Again keeps the round and its secrets (follow-up 2, 9) -----------------------

def _secrets(world) -> tuple:
    """The round's sid, round and keys, as every entry the list holds for
    it names them: one set, however often it was recorded."""
    (held,) = {(e.sid, e.round, e.s_sub, e.status_key) for e in world.list.entries}
    return held


@pytest.mark.parametrize("fail", [
    lambda w: w.server.inject("submit", DROP_BEFORE),               # no connection
    lambda w: w.server.inject("submit", LOST),                      # never received
    lambda w: w.server.inject("submit", Fault(503, "unavailable")),
    lambda w: w.server.inject("submit", Fault(429, "rate_limited", retry_after=60)),
], ids=["no connection", "not received", "unavailable", "a limit"])
def test_try_again_after_a_submit_that_did_not_go_sends_the_same_round_and_secrets(fail):
    # The POST is not tried again quietly (each try is a signer prompt):
    # Try Again sends the very body again, the same sid, round, status
    # key and wraps, with a new signature only.
    world = World(pictures=False)
    fail(world)
    progress = world.go()
    assert progress.step is Step.FAILED and progress.failure.retry is Step.SIGNING
    before = _secrets(world)
    world.settle(seconds=60)
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED
    first, second = world.posted()[0], world.posted()[-1]
    assert first == second and _secrets(world) == before
    assert second["submission_id"] == before[0] and second["round"] == before[1]
    assert len(world.nip98()) >= 2                      # a new signature for each try
    assert len(world.list.entries) == 1                 # recorded once


def test_try_again_after_the_signer_app_declined_keeps_the_round_and_secrets():
    world = World(signer=HeldSigner(sk("writer")), pictures=False)
    world.submitting.start()
    world.settle()
    world.signer.held.pop(0)[2]("declined")         # the person says no on the phone
    world.settle()
    assert world.submitting.progress.failure.retry is Step.SIGNING
    before = _secrets(world)
    world.submitting.retry()
    world.settle()
    world.signer.release()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED
    assert _secrets(world) == before and len(world.posted()) == 1
    assert world.posted()[0]["submission_id"] == before[0]


def test_try_again_after_the_key_changed_keeps_the_round_and_secrets():
    # epoch: the editorial team's key moved on; the wraps are made anew
    # for the new key, never the sid, the round or the status key.
    world = World(pictures=False)
    world.server.inject("submit", Fault(409, "epoch"))
    progress = world.go()
    assert progress.failure.retry is Step.KEY
    before = _secrets(world)
    world.submitting.retry()
    world.settle()
    assert world.submitting.progress.step is Step.SUBMITTED
    first, second = world.posted()[0], world.posted()[-1]
    assert _secrets(world) == before
    assert (second["submission_id"], second["round"], second["status_key_hash"]) == (
        first["submission_id"], first["round"], first["status_key_hash"])
