# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The window's side of submitting (nostr/ui/standup_submit.py).

Pinned here: the encrypted pictures go to the writer's media servers
through the Media Library's store, answered by hash only once every one
is there, and a file the server names by another hash is a failure;
the round a text goes in is never guessed (a list or a server that
cannot be read says so, as a guess could overwrite the key of a
submission sent before); signing out stops what is on its way, forgets
the media keys and the list at once and writes what was read before
its list stops; a submission going on in the background says in the
status bar how it ends, "Eingereicht" only once it is in; the small
cover is a JPEG of at most 128 px and 24 KB."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import List

import pytest
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import i18n
from nostr.list_results import ListRead, ReadStatus
from nostr.standup import formats, words
from nostr.standup.answers import LookupAnswer
from nostr.standup.drafting import ROUND_UNKNOWN, encrypt_pictures, LocalPicture
from nostr.standup.entries import SubmissionEntry
from nostr.standup.lists import SUBMISSIONS_MISSING_PAGE, SUBMISSIONS_UNREADABLE
from nostr.standup.submitting import MessageStatus, Progress, Step
from nostr.ui import standup_submit
from nostr.ui.standup_submit import BlossomUploads, StandupWriter, thumbnail

WRITER = "a1" * 32
PUBLICATION = f"30078:{'b2' * 32}:magazin"


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def german():
    i18n.install("de")
    yield
    i18n.install("en")


# -- the pictures ------------------------------------------------------------

class Store(QObject):
    """The Media Library's store, as far as uploads go."""

    upload_progress = Signal(str, int, int)
    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.sent: List[tuple] = []
        self.cancelled: List[str] = []

    def upload_bytes(self, body, *, name, mime_type, force, submission) -> None:
        assert force and submission
        self.sent.append((name, body, mime_type))

    def cancel_upload(self, name) -> bool:
        self.cancelled.append(name)
        return True

    def finish(self, name: str, sha: str, servers=("https://one.example", "https://two.example")):
        self.upload_finished.emit(name, SimpleNamespace(
            hash=sha, urls=[{"server": s, "url": f"{s}/{sha}"} for s in servers]))


def _pictures(count: int):
    return encrypt_pictures([LocalPicture(key=f"k{i}", name=f"{i}.jpg", mime="image/jpeg",
                                          data=bytes([i]) * 2000) for i in range(count)])


def _upload(store, pictures):
    seen = {"progress": [], "done": [], "failed": []}
    stop = BlossomUploads(store)(pictures, seen["progress"].append, seen["done"].append,
                                 seen["failed"].append)
    return seen, stop


def test_the_encrypted_files_go_up_through_the_library_and_are_answered_by_hash():
    store, pictures = Store(), _pictures(2)
    seen, _stop = _upload(store, pictures)
    assert [body for _name, body, _mime in store.sent] == [p.cipher for p in pictures]
    assert {mime for *_rest, mime in store.sent} == {"application/octet-stream"}
    first, second = (name for name, *_rest in store.sent)
    store.upload_progress.emit(first, pictures[0].encryptor.cipher_size, 0)
    assert seen["progress"][-1] == pytest.approx(0.5, abs=0.01)
    store.finish(first, pictures[0].sha256)
    assert seen["done"] == []                       # not before every file is there
    store.finish(second, pictures[1].sha256, servers=("https://one.example",))
    assert seen["done"] == [{pictures[0].sha256: ("https://one.example", "https://two.example"),
                             pictures[1].sha256: ("https://one.example",)}]


def test_a_file_the_server_names_by_another_hash_fails_and_the_others_stop():
    store, pictures = Store(), _pictures(2)
    seen, _stop = _upload(store, pictures)
    first, second = (name for name, *_rest in store.sent)
    store.finish(first, "00" * 32)
    assert seen["failed"] and seen["done"] == []
    assert set(store.cancelled) == {first, second}
    store.finish(second, pictures[1].sha256)        # heard no more
    assert seen["done"] == []


def test_a_file_the_server_misses_goes_up_again_through_the_real_library(tmp_path):
    # RG-M2b 4, against the real MediaStore: its own record of the file
    # never stands in for the server's word that it is missing.
    from nostr.blossom.client import BlossomClient
    from nostr.blossom.settings import BlossomSettings
    from nostr.blossom.store import MediaStore
    from tests.blossom_fakes import (SERVER, FakeBlossomServer, FakeNam, FakeProfile,
                                     FakeSessionPool, FakeSigner)
    settings = BlossomSettings(tmp_path / "servers.json")
    settings.set_custom_servers([SERVER])
    server = FakeBlossomServer(SERVER)
    nam = FakeNam(None, responder=server)
    store = MediaStore(session_pool=FakeSessionPool(FakeSigner()), profile_provider=FakeProfile,
                       settings=settings, client=BlossomClient(nam=nam))
    pictures = _pictures(1)
    for _attempt in range(2):                       # sent, then the receipt says missing
        seen, _stop = _upload(store, pictures)
        nam.settle()
        assert seen["done"] == [{pictures[0].sha256: (SERVER,)}]
    assert server.uploads == [pictures[0].cipher] * 2
    assert all(body[:4] == b"SMF1" for body in server.uploads)     # never the plaintext


def test_stopping_cancels_what_is_on_its_way():
    store, pictures = Store(), _pictures(1)
    seen, stop = _upload(store, pictures)
    stop()
    assert store.cancelled == [store.sent[0][0]]
    store.finish(store.sent[0][0], pictures[0].sha256)
    assert seen["done"] == []


# -- the round -----------------------------------------------------------------

def _sid(round_: int) -> str:
    return f"{round_:x}".rjust(64, "d")


def _key(round_: int) -> str:
    return f"{round_:x}".rjust(64, "c")


def _entry(round_: int = 1) -> SubmissionEntry:
    return SubmissionEntry(sid=_sid(round_), publication=PUBLICATION, server="https://s.example",
                           role="writer", s_sub="e5" * 32, status_key=_key(round_), title="T",
                           kind="article", d="ein-jahr", round=round_, revision=1,
                           revision_hash="f6" * 32, updated_at=1)


class FakeList:
    def __init__(self, entries=(), readable=True, complete=True) -> None:
        self.loaded = False
        self.complete = complete
        self.reads = 0
        self._entries = list(entries)
        self._readable = readable
        self.stopped = False
        self.forgotten = []

    def load(self, on_done) -> None:
        self.reads += 1
        self.loaded = self._readable
        on_done(ListRead(ReadStatus.COMPLETE if self._readable else ReadStatus.UNREADABLE))

    def entries(self):
        return self._entries

    def record(self, entry, on_done) -> None:
        self._entries.append(entry)

    def entry(self, sid):
        return next((e for e in self._entries if e.sid == sid), None)

    def forget(self, entry, on_done) -> None:
        self.forgotten.append(entry)
        on_done(SimpleNamespace(stored=True))

    def stop(self) -> None:
        self.stopped = True


class FakeApi:
    """The lookup: ``states`` per round (one state: for every round
    asked), None where the server stored nothing."""

    def __init__(self, states=None, *, unreadable=False, fails=False, other_sid=False) -> None:
        self.states, self.unreadable, self.fails = states, unreadable, fails
        self.other_sid = other_sid
        self.asked = []
        self.stopped = False

    def lookup(self, items, on_success, on_failure) -> None:
        keys = [key for key, _since in items]
        self.asked.append(keys)
        if self.fails:
            on_failure(RuntimeError("offline"))
            return
        if self.unreadable:
            on_success(LookupAnswer(found={}, unreadable=frozenset(keys[:1])))
            return
        found = {}
        for key in keys:
            round_ = int(key.lstrip("c") or "0", 16)
            state = self.states.get(round_) if isinstance(self.states, dict) else self.states
            if state is not None:
                found[key] = SimpleNamespace(
                    submission=SimpleNamespace(state=state),
                    receipt=SimpleNamespace(submission_id="00" * 32 if self.other_sid
                                            else _sid(round_)))
        on_success(LookupAnswer(found=found, unreadable=frozenset()))

    def stop(self) -> None:
        self.stopped = True

    def close_session(self, on_done) -> None:
        assert not self.stopped                 # revoked before the calls stop
        self.closed = on_done


def _writer(submissions, api, **more) -> StandupWriter:
    more.setdefault("deleteLater", lambda: None)
    account = SimpleNamespace(writer=WRITER, submissions=submissions, api=api, runs=[],
                              list_problem="", **more)
    return StandupWriter(session_pool=None, relay_pool=None, relay_directory=None,
                         media_store=None, server="https://s.example", uploads=lambda *a: None,
                         accounts=lambda _profile: account)


def _answer(writer):
    answers = []
    writer.round_for(SimpleNamespace(user_pubkey=WRITER), PUBLICATION, "ein-jahr",
                     answers.append)
    QApplication.processEvents()
    assert len(answers) == 1
    return answers[0]


def _round(writer) -> tuple:
    answer = _answer(writer)
    return answer.round, answer.words


@pytest.mark.parametrize("entries, api, expected", [
    ((), FakeApi(), (1, "")),                               # never submitted there
    ((_entry(1),), FakeApi(None), (1, "")),                 # nothing stored: send again
    ((_entry(1),), FakeApi("declined"), (2, "")),           # closed: the next round
    ((_entry(1), _entry(2)), FakeApi("withdrawn"), (3, "")),
    ((_entry(1),), FakeApi("in_review"), (None, "")),       # still open there
    ((_entry(2), _entry(1)), FakeApi({1: "in_review", 2: None}), (None, "")),
], ids=["none", "unsent", "declined", "withdrawn", "open", "an earlier round open"])
def test_the_round_follows_what_the_server_says_of_every_entry(entries, api, expected):
    # Contract 2: every entry of the work is asked about, in one lookup.
    assert _round(_writer(FakeList(entries), api)) == expected
    if entries:
        assert sorted(api.asked[0]) == sorted(e.status_key for e in entries)


def test_an_entry_the_server_has_no_receipt_for_is_sent_again_or_discarded():
    entry = _entry(1)
    assert _answer(_writer(FakeList([entry]), FakeApi(None))).unsent is entry
    assert _answer(_writer(FakeList([entry]), FakeApi("declined"))).unsent is None
    submissions = FakeList([entry])
    discarded = []
    _writer(submissions, FakeApi()).discard(SimpleNamespace(user_pubkey=WRITER), entry,
                                            discarded.append)
    assert submissions.forgotten == [entry] and discarded == [True]


def test_one_run_per_work_and_publication(german):
    from nostr.standup.drafting import ArticleDraft, Choice
    from nostr.ui.standup_submit import RoundAnswer
    writer = _writer(FakeList(), FakeApi())
    profile = SimpleNamespace(user_pubkey=WRITER)
    draft = ArticleDraft(d="ein-jahr", title="T", summary="", content="x")
    choice = Choice(publication=PUBLICATION, name="M", signer="writer", rights=True)
    first = writer.submit(profile, draft, choice, RoundAnswer(1))
    assert writer.submit(profile, draft, choice, RoundAnswer(1)) is first
    assert writer.running(profile, "ein-jahr") is first
    assert _round(writer) == (None, "")             # while it is on its way: still open
    other = Choice(publication=f"30078:{'c4' * 32}:x", name="N", signer="writer", rights=True)
    assert writer.submit(profile, draft, other, RoundAnswer(1)) is not first


@pytest.mark.parametrize("submissions, api, said", [
    (FakeList([_entry()]), FakeApi(fails=True), ROUND_UNKNOWN),           # no answer
    (FakeList([_entry()]), FakeApi(unreadable=True), ROUND_UNKNOWN),      # has something
    (FakeList([_entry()]), FakeApi("declined", other_sid=True), ROUND_UNKNOWN),  # another's
    (FakeList(readable=False), FakeApi(), SUBMISSIONS_UNREADABLE),
    (FakeList(complete=False), FakeApi(), SUBMISSIONS_MISSING_PAGE),      # a page missing
], ids=["no answer", "unreadable", "another submission", "list unreadable", "list incomplete"])
def test_a_round_that_cannot_be_told_is_not_guessed(submissions, api, said):
    # RG-M2b 2: never "already submitted", never round 1 by default.
    assert _round(_writer(submissions, api)) == (None, said)


def test_a_list_that_could_not_be_read_is_said_again_without_asking_again():
    submissions = FakeList(readable=False)
    writer = _writer(submissions, FakeApi())
    assert _round(writer) == _round(writer) == (None, SUBMISSIONS_UNREADABLE)
    assert submissions.reads == 1


def test_a_list_that_failed_for_the_publications_is_not_read_again_for_the_round():
    # RG-M2b 11: the sheet asks for the publications first; a second read
    # for the round would be a second signer prompt.
    submissions = FakeList(readable=False)
    writer = _writer(submissions, FakeApi(),
                     destinations=SimpleNamespace(find=lambda found, _failed: found([])))
    got = []
    writer.destinations(SimpleNamespace(user_pubkey=WRITER),
                        lambda destinations, used: got.append(used), print)
    QApplication.processEvents()
    assert got == [[]] and submissions.reads == 1
    assert _round(writer) == (None, SUBMISSIONS_UNREADABLE)
    assert submissions.reads == 1


# -- sign-out ---------------------------------------------------------------------

class ReadStateList(FakeList):
    def __init__(self, log) -> None:
        super().__init__()
        self.log = log
        self.answer = None

    def before_sign_out(self, on_done) -> None:
        self.log.append("write what was read")
        self.answer = on_done

    def stop(self) -> None:
        self.log.append("read state stops")


def test_signing_out_forgets_at_once_and_writes_what_was_read_first():
    log: List[str] = []
    keys = SimpleNamespace(clear=lambda: log.append("media keys forgotten"))
    run = SimpleNamespace(submitting=SimpleNamespace(stop=lambda: log.append("run stops")),
                          finished=True, progress=Progress(Step.SUBMITTED))
    read_state = ReadStateList(log)
    submissions = FakeList()
    api = FakeApi()
    writer = _writer(submissions, api, media_keys=keys, read_state=read_state,
                     destinations=SimpleNamespace(stop=lambda: log.append("lookups stop")))
    writer.account(SimpleNamespace(user_pubkey=WRITER)).runs.append(run)
    done = []
    writer.stop(lambda: done.append(True))
    assert log == ["run stops", "lookups stop", "media keys forgotten", "write what was read"]
    assert submissions.stopped and api.stopped and not done
    read_state.answer(None)
    assert log[-1] == "read state stops" and not done     # the session is still revoked
    api.closed()
    assert done == [True]


def test_signing_out_revokes_the_session_on_the_server():
    # RG-M2a 3: the production caller of close_session.
    from tests.test_standup_client import World
    world = World()
    api = world.api()
    world.ok(api.my_submissions)                          # a session is open
    assert len(world.server.sessions) == 1
    read_state = ReadStateList([])
    writer = _writer(FakeList(), api, media_keys=SimpleNamespace(clear=lambda: None),
                     read_state=read_state, destinations=SimpleNamespace(stop=lambda: None))
    writer.account(SimpleNamespace(user_pubkey=WRITER))
    done = []
    writer.stop(lambda: done.append(True))
    read_state.answer(None)
    world.nam.settle()
    assert world.server.sessions == {} and done == [True]


def _stopping(*progresses):
    """A writer whose account has a run at each of ``progresses``."""
    from nostr.standup.submitting import PHASE_OF, Phase
    writer = _writer(FakeList(), FakeApi(), media_keys=SimpleNamespace(clear=lambda: None),
                     read_state=ReadStateList([]), destinations=SimpleNamespace(stop=lambda: None))
    account = writer.account(SimpleNamespace(user_pubkey=WRITER))
    for progress in progresses:
        account.runs.append(SimpleNamespace(
            submitting=SimpleNamespace(stop=lambda: None), progress=progress,
            finished=PHASE_OF[progress.step] in (Phase.DONE, Phase.FAILED, Phase.CANCELLED),
            choice=SimpleNamespace(name="Einundzwanzig Magazin")))
    return writer


@pytest.mark.parametrize("progresses, said", [
    ((Progress(Step.UPLOADING),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed. "
     "Nothing was sent."),
    ((Progress(Step.SIGNING),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed. "
     "Nothing was sent."),
    ((Progress(Step.SENDING, sent=True),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before the "
     "server confirmed it. Open Submit to a Publication with that account to see whether it "
     "arrived."),
    ((Progress(Step.CHECKING, sent=True),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before the "
     "server confirmed it. Open Submit to a Publication with that account to see whether it "
     "arrived."),
    ((Progress(Step.REUPLOADING, sent=True, receipt=object()),),
     "Your submission to Einundzwanzig Magazin is in, but a picture was still being uploaded "
     "when the account changed."),
    ((Progress(Step.SUBMITTED, sent=True, receipt=object(), message=MessageStatus.SENDING),),
     "Your submission to Einundzwanzig Magazin is in, but your message may not have reached "
     "the editorial team: the account changed."),
    ((Progress(Step.UPLOADING), Progress(Step.SENDING, sent=True)),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before the "
     "server confirmed it. Open Submit to a Publication with that account to see whether it "
     "arrived. 1 other submission stopped as well."),
    ((Progress(Step.SUBMITTED, sent=True, receipt=object()), Progress(Step.FAILED)), None),
], ids=["uploading", "signing", "sending", "checking", "pictures", "message", "two", "none"])
def test_a_submission_stopped_with_its_account_is_said_by_what_reached_the_server(
        progresses, said):
    # RG-M2b 9: sign-out, another account or a signer paired anew stopped
    # runs without a word; the words follow what was sent (lesson 8).
    writer = _stopping(*progresses)
    told, stopped = [], []
    writer.status.connect(told.append)
    writer.stopped.connect(stopped.append)
    writer.stop()
    assert stopped == [WRITER] and told == []      # after the account change is done
    QApplication.processEvents()
    assert told == ([said] if said else [])


def test_without_an_account_nothing_is_said_to_have_stopped():
    writer = _writer(FakeList(), FakeApi())
    told, stopped = [], []
    writer.status.connect(told.append)
    writer.stopped.connect(stopped.append)
    writer.stop()
    QApplication.processEvents()
    assert told == [] and stopped == []


def test_the_lookup_of_a_run_checks_the_receipt_against_the_body_it_sent(monkeypatch):
    # RG-M2b 11: the production port, not only the table's fake.
    import nostr.ui.standup_submit as module
    asked = []
    monkeypatch.setattr(module, "resolve_unconfirmed",
                        lambda api, entry, on_done, on_failure, **kw: asked.append((entry, kw)))
    writer = _writer(FakeList(), FakeApi())
    account = writer.account(SimpleNamespace(user_pubkey=WRITER))
    sealed = SimpleNamespace(entry=_entry(), body={"submission_id": _sid(1)})
    writer._ports(account, PUBLICATION).lookup(sealed, print, print)
    assert asked == [(sealed.entry, {"body": sealed.body})]


# -- in the background --------------------------------------------------------------

class Run(QObject):
    changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.choice = SimpleNamespace(name="Einundzwanzig Magazin")
        self.finished = False


def test_in_the_background_the_status_bar_says_submitted_only_once_it_is_in(german):
    writer, run, said = _writer(FakeList(), FakeApi()), Run(), []
    writer.status.connect(said.append)
    writer.in_background(run)
    run.changed.emit(Progress(Step.SENDING))
    run.changed.emit(Progress(Step.CHECKING))
    assert said == ["Wird eingereicht …"]
    run.changed.emit(Progress(Step.SUBMITTED))
    assert said[-1] == "Eingereicht. Im Eingang von Einundzwanzig Magazin."


def test_in_the_background_a_failure_is_said_in_its_words(german):
    from nostr.standup.submitting import Failure
    writer, run, said = _writer(FakeList(), FakeApi()), Run(), []
    writer.status.connect(said.append)
    writer.in_background(run)
    run.changed.emit(Progress(Step.FAILED, failure=Failure(words.NOT_SENT, Step.SIGNING)))
    assert said[-1] == "Nicht gesendet. Keine Verbindung zum Server."


# -- the small cover ---------------------------------------------------------------

def _jpeg(width: int, height: int) -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    for y in range(height):
        for x in range(0, width, 7):
            image.setPixelColor(x, y, QColor((x * 7) % 255, (y * 3) % 255, (x * y) % 255))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def test_the_small_cover_is_a_jpeg_of_at_most_128_points_and_24_kb():
    thumb = thumbnail(_jpeg(1600, 900))
    raw = base64.b64decode(thumb["data"])
    assert thumb["mime"] == "image/jpeg" and raw[:2] == b"\xff\xd8"
    assert len(raw) <= formats.MAX_THUMB_BYTES
    image = QImage.fromData(raw)
    assert max(image.width(), image.height()) == standup_submit.THUMB_SIDE
    assert thumbnail(b"not a picture") is None


# -- the first message ----------------------------------------------------------------

def test_the_first_message_is_sealed_by_the_writer_and_wrapped_to_the_submissions_room():
    from nostr import crypto as nostr_crypto
    from nostr.standup import conversation
    from tests.standup_fakes import SignAs
    from tests.standup_vectors import pk, sk

    s_sub = bytes(range(1, 33))
    sid = "ab" * 32
    sealed = SimpleNamespace(body={"p_sub": nostr_crypto.get_public_key(s_sub).hex()},
                             s_sub=s_sub, submission_id=sid)
    signer, sent, said = SignAs(sk("writer")), [], []

    def send_message(submission_id, wrap, *, kind_hint, on_success, on_failure):
        sent.append((submission_id, wrap, kind_hint))
        on_success(7)                               # only a seq says it is there

    account = SimpleNamespace(
        writer=pk("writer"), api=SimpleNamespace(send_message=send_message),
        signer=SimpleNamespace(sign_event=lambda unsigned, *, on_success, on_failure:
                               signer(unsigned, on_success, on_failure)))
    StandupWriter._send_message(account, sealed, "Passt zu eurer Reihe.",
                                lambda: said.append("sent"), lambda: said.append("not sent"))
    (submission_id, wrap, kind_hint), = sent
    opened = conversation.open_item(wrap, s_sub, sid)
    assert (submission_id, kind_hint, said) == (sid, "message", ["sent"])
    assert opened.sender == pk("writer")
    assert opened.rumor["content"] == "Passt zu eurer Reihe."
    assert len(signer.requests) == 1                # one prompt: the seal


def test_a_message_with_a_secret_key_in_it_is_never_sent():
    from nostr.bech32 import encode_nsec
    from tests.standup_vectors import pk, sk

    s_sub = bytes(range(1, 33))
    sent, said = [], []
    account = SimpleNamespace(writer=pk("writer"), api=SimpleNamespace(
        send_message=lambda *a, **k: sent.append(a)), signer=None)
    sealed = SimpleNamespace(body={"p_sub": "00" * 32}, s_sub=s_sub, submission_id="ab" * 32)
    nsec = encode_nsec(sk("writer").hex())
    StandupWriter._send_message(account, sealed, f"mein Schlüssel {nsec}",
                                lambda: said.append("sent"), lambda: said.append("not sent"))
    QApplication.processEvents()
    assert sent == [] and said == ["not sent"]


def test_an_account_is_wired_to_the_app_and_left_whole_at_sign_out():
    from nostr.profiles import Profile
    from tests.outbox_fakes import FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool

    writer = StandupWriter(session_pool=FakeSessionPool(FakeClient()), relay_pool=FakePool(),
                           relay_directory=FakeRelayDirectory(), media_store=Store(),
                           server="https://standup.example")
    profile = Profile(user_pubkey=WRITER, bunker_pubkey="b" * 64,
                      bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64)
    account = writer.account(profile)
    assert writer.account(profile) is account               # one per account
    assert account.api.server == "https://standup.example"
    assert account.submissions.account == WRITER == account.read_state.account
    gone = []
    account.api.destroyed.connect(lambda *_a: gone.append("api"))
    done = []
    writer.stop(lambda: done.append(True))
    QApplication.processEvents()
    assert done == [True]                                    # nothing read: nothing to write
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert gone == ["api"]                                   # deleted with its account
    assert writer.account(profile) is not account            # a new one after signing out
