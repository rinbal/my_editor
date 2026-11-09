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
from PySide6.QtCore import QBuffer, QIODevice, QObject, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import i18n
from nostr.list_results import ListRead, ReadStatus
from nostr.standup import formats, words
from nostr.standup.answers import LookupAnswer
from nostr.standup.drafting import ROUND_UNKNOWN, encrypt_pictures, LocalPicture
from nostr.standup.lists import SUBMISSIONS_UNREADABLE, SubmissionEntry
from nostr.standup.submitting import Progress, Step
from nostr.ui import standup_submit
from nostr.ui.standup_submit import BlossomUploads, StandupWriter, thumbnail

WRITER = "a1" * 32
PUBLICATION = f"30078:{'b2' * 32}:magazin"
KEY = "c3" * 32


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

    def upload_bytes(self, body, *, name, mime_type) -> None:
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


def test_stopping_cancels_what_is_on_its_way():
    store, pictures = Store(), _pictures(1)
    seen, stop = _upload(store, pictures)
    stop()
    assert store.cancelled == [store.sent[0][0]]
    store.finish(store.sent[0][0], pictures[0].sha256)
    assert seen["done"] == []


# -- the round -----------------------------------------------------------------

def _entry(round_: int = 1) -> SubmissionEntry:
    return SubmissionEntry(sid="d4" * 32, publication=PUBLICATION, server="https://s.example",
                           role="writer", s_sub="e5" * 32, status_key=KEY, title="T",
                           kind="article", d="ein-jahr", round=round_, revision=1,
                           revision_hash="f6" * 32, updated_at=1)


class FakeList:
    def __init__(self, entries=(), readable=True) -> None:
        self.loaded = False
        self._entries = list(entries)
        self._readable = readable
        self.stopped = False

    def load(self, on_done) -> None:
        self.loaded = self._readable
        on_done(ListRead(ReadStatus.COMPLETE if self._readable else ReadStatus.UNREADABLE))

    def entries(self):
        return self._entries

    def stop(self) -> None:
        self.stopped = True


class FakeApi:
    def __init__(self, state=None, *, unreadable=False, fails=False) -> None:
        self.state, self.unreadable, self.fails = state, unreadable, fails
        self.stopped = False

    def lookup(self, items, on_success, on_failure) -> None:
        if self.fails:
            on_failure(RuntimeError("offline"))
        elif self.unreadable:
            on_success(LookupAnswer(found={}, unreadable=frozenset({KEY})))
        elif self.state is None:
            on_success(LookupAnswer(found={}, unreadable=frozenset()))
        else:
            item = SimpleNamespace(submission=SimpleNamespace(state=self.state))
            on_success(LookupAnswer(found={KEY: item}, unreadable=frozenset()))

    def stop(self) -> None:
        self.stopped = True


def _writer(submissions, api, **more) -> StandupWriter:
    account = SimpleNamespace(writer=WRITER, submissions=submissions, api=api, runs=[], **more)
    return StandupWriter(session_pool=None, relay_pool=None, relay_directory=None,
                         media_store=None, server="https://s.example", uploads=lambda *a: None,
                         accounts=lambda _profile: account)


def _round(writer) -> tuple:
    answers = []
    writer.round_for(SimpleNamespace(user_pubkey=WRITER), PUBLICATION, "ein-jahr",
                     lambda round_, said: answers.append((round_, said)))
    QApplication.processEvents()
    assert len(answers) == 1
    return answers[0]


@pytest.mark.parametrize("entries, api, expected", [
    ((), FakeApi(), (1, "")),                               # never submitted there
    ((_entry(1),), FakeApi(None), (1, "")),                 # nothing stored: send again
    ((_entry(1),), FakeApi("declined"), (2, "")),           # closed: the next round
    ((_entry(1), _entry(2)), FakeApi("withdrawn"), (3, "")),
    ((_entry(1),), FakeApi("in_review"), (None, "")),       # still open there
    ((_entry(1),), FakeApi(unreadable=True), (None, "")),   # the server has something
])
def test_the_round_follows_the_list_and_the_server(entries, api, expected):
    assert _round(_writer(FakeList(entries), api)) == expected


def test_a_round_that_cannot_be_told_is_not_guessed():
    assert _round(_writer(FakeList([_entry()]), FakeApi(fails=True))) == (None, ROUND_UNKNOWN)
    assert _round(_writer(FakeList(readable=False), FakeApi())) == (
        None, SUBMISSIONS_UNREADABLE)


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
    run = SimpleNamespace(submitting=SimpleNamespace(stop=lambda: log.append("run stops")))
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
    assert log[-1] == "read state stops" and done == [True]


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
    done = []
    writer.stop(lambda: done.append(True))
    assert done == [True]                                    # nothing read: nothing to write
    assert writer.account(profile) is not account            # a new one after signing out
