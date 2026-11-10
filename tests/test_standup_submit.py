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
from types import MethodType, SimpleNamespace
from typing import List

import pytest
from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, QTimer, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import i18n
from nostr.list_results import ListRead, ReadStatus
from nostr.standup import formats, words
from nostr.standup.errors import Code, StandupError
from nostr.standup.watcher import AT_THE_END
from nostr.standup.answers import LookupAnswer
from nostr import failures
from nostr.standup.drafting import ROUND_UNKNOWN, LocalPicture, encrypt_pictures
from nostr.standup.entries import SubmissionEntry
from nostr.standup.lists import SUBMISSIONS_UNREADABLE
from nostr.standup.submitting import Failure, Held, MessageStatus, Progress, Step
from nostr.ui import standup_message, standup_run, standup_submit
from nostr.ui.standup_account import Account
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


class Later:
    """The writer's ``_later``: a turn (0 ms) as Qt's, a wait held until
    the test runs it (lesson 34: no wall-clock waits)."""

    def __init__(self) -> None:
        self.waiting = []                  # (ms, call)

    def __call__(self, ms, call) -> None:
        if ms <= 0:
            QTimer.singleShot(0, call)
        else:
            self.waiting.append((ms, call))

    def run(self) -> list:
        """Run what waits, in order; the delays it waited."""
        ran = []
        while self.waiting:
            ms, call = self.waiting.pop(0)
            ran.append(ms)
            call()
            QApplication.processEvents()
        return ran


LATER = Later()


@pytest.fixture(autouse=True)
def later(monkeypatch):
    global LATER
    LATER = Later()
    monkeypatch.setattr(standup_submit, "_later", LATER)
    return LATER


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


def test_a_mirror_that_refuses_a_picture_says_nothing_and_the_first_server_counts(tmp_path):
    # Lessons 36: some servers failing is normal. The picture is on the
    # first server; the mirror's refusal is never a failure of the run.
    from PySide6.QtCore import QUrl
    from nostr.blossom.client import BlossomClient
    from nostr.blossom.settings import BlossomSettings
    from nostr.blossom.store import MediaStore
    from tests.blossom_fakes import (MIRROR, SERVER, FakeBlossomServer, FakeNam, FakeProfile,
                                     FakeSessionPool, FakeSigner)
    servers = {QUrl(SERVER).host(): FakeBlossomServer(SERVER),
               QUrl(MIRROR).host(): FakeBlossomServer(MIRROR, fail_with=500,
                                                      fail_reason="disk full")}
    settings = BlossomSettings(tmp_path / "servers.json")
    settings.set_custom_servers([SERVER, MIRROR])
    nam = FakeNam(None, responder=lambda verb, request, body: servers[request.url().host()](
        verb, request, body))
    store = MediaStore(session_pool=FakeSessionPool(FakeSigner()), profile_provider=FakeProfile,
                       settings=settings, client=BlossomClient(nam=nam))
    pictures = _pictures(1)
    seen, _stop = _upload(store, pictures)
    nam.settle()
    assert seen["failed"] == [] and seen["done"] == [{pictures[0].sha256: (SERVER,)}]
    assert any(QUrl(SERVER).host() != request.url().host() for _v, request, _b in nam.calls)


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
                           revision_hash="f6" * 32, envelope_sha256="a7" * 32,
                           wraps_sha256={"redaktion": "b8" * 32, "writer": "c9" * 32},
                           updated_at=1)


class FakeList:
    """SubmissionsList's side the writer uses. ``arriving``: entries
    another device wrote, found by the next read and merged as the real
    list merges them (entries.merge_items)."""

    def __init__(self, entries=(), readable=True, complete=True) -> None:
        self.loaded = False
        self.complete = complete
        self.reads = 0
        self._entries = list(entries)
        self._readable = readable
        self.stopped = False
        self.forgotten = []
        self.marked = []
        self.arriving = []

    def load(self, on_done) -> None:
        from nostr.standup.entries import SubmissionItems, merge_items
        self.reads += 1
        self.loaded = self._readable
        if self._readable and self.arriving:
            merged = merge_items(SubmissionItems(tuple(self.arriving)),
                                 SubmissionItems(tuple(self._entries)))
            self._entries, self.arriving = list(merged.entries), []
        on_done(ListRead(ReadStatus.COMPLETE if self._readable else ReadStatus.UNREADABLE))

    def mark_stored(self, entries, on_done=lambda _results: None) -> None:
        # As SubmissionsList.mark_stored: the entry held with these keys.
        from dataclasses import replace
        from nostr.standup.entries import same_keys
        for entry in entries:
            self.marked.append(entry)
            self._entries = [replace(e, stored=True) if e.sid == entry.sid
                             and same_keys(e, entry) else e for e in self._entries]

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
    asked), None where the server stored nothing. Each answer takes
    ``size`` bytes."""

    def __init__(self, states=None, *, unreadable=False, fails=False, other_sid=False,
                 server="https://s.example", size=100, missing=()) -> None:
        self.states, self.unreadable, self.fails = states, unreadable, fails
        self.missing = tuple(missing)               # files every receipt misses
        self.other_sid = other_sid
        self.server, self.size = server, size
        self.asked = []
        self.limits = []
        self.since = set()
        self.stopped = False

    def lookup(self, items, on_success, on_failure, *, limit=None, on_bytes=None) -> None:
        keys = [key for key, _since in items]
        self.asked.append(keys)
        self.limits.append(limit)
        self.since.update(since for _key, since in items)
        if on_bytes is not None:
            on_bytes(self.size)
        if self.fails:
            # As StandupApi answers: a StandupError, never another error.
            from nostr.standup.errors import Code, StandupError
            on_failure(self.fails if isinstance(self.fails, StandupError)
                       else StandupError(Code.OFFLINE))
            return
        if self.unreadable:
            on_success(LookupAnswer(found={}, unreadable=frozenset(keys[:1])))
            return
        found = {}
        for key in keys:
            round_ = int(key.lstrip("c") or "0", 16)
            state = self.states.get(round_) if isinstance(self.states, dict) else self.states
            if state is not None:
                sid = "00" * 32 if self.other_sid else _sid(round_)
                found[key] = SimpleNamespace(
                    submission=SimpleNamespace(state=state, submission_id=sid, round=round_),
                    receipt=SimpleNamespace(submission_id=sid, blobs_missing=self.missing))
        on_success(LookupAnswer(found=found, unreadable=frozenset()))

    def stop(self) -> None:
        self.stopped = True

    def close_session(self, on_done) -> None:
        assert not self.stopped                 # revoked before the calls stop
        self.closed = on_done


def _writer(submissions, api, *, marks=None, **more) -> StandupWriter:
    more.setdefault("deleteLater", lambda: None)
    more.setdefault("client_for", lambda _server: api)
    more.setdefault("clients", {})
    account = SimpleNamespace(writer=WRITER, submissions=submissions, api=api, runs=[],
                              list_problem="", stopped=False, read_again=False,
                              checking=False, check_failed=False, _reading=None,
                              _later=LATER, **more)
    account.read_whole = MethodType(Account.read_whole, account)    # the real one read
    return StandupWriter(session_pool=None, relay_pool=None, relay_directory=None,
                         media_store=None, server="https://s.example", uploads=lambda *a: None,
                         accounts=lambda _profile: account, marks=marks)


def _answer(writer):
    """The round's answer, once the quiet tries that wait for it ran."""
    answers = []
    writer.round_for(SimpleNamespace(user_pubkey=WRITER), PUBLICATION, "ein-jahr",
                     answers.append)
    QApplication.processEvents()
    LATER.run()
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


def test_each_entry_is_asked_of_the_server_it_names():
    # RG-M2r 9: an entry of another server was asked of this one, read as
    # never stored and sent again with its keys.
    from dataclasses import replace
    here, there = FakeApi("declined"), FakeApi("in_review", server="https://other.example")
    elsewhere = replace(_entry(2), server=there.server)
    writer = _writer(FakeList([_entry(1), elsewhere]), here,
                     client_for={here.server: here, there.server: there}.get)
    assert _round(writer) == (None, "")             # still open there
    assert (here.asked, there.asked) == ([[_key(1)]], [[_key(2)]])
    assert here.since == there.since == {AT_THE_END}


def test_a_lookup_never_asks_one_server_about_anothers_entry():
    from dataclasses import replace
    from nostr.standup.watcher import look_up_all
    with pytest.raises(ValueError):
        look_up_all(FakeApi(), [replace(_entry(1), server="https://other.example")],
                    print, print)


PROFILE = SimpleNamespace(user_pubkey=WRITER)


def _lock(writer, d="ein-jahr", *, in_use=True, profile=PROFILE):
    return writer.link_name(profile, d, in_use=in_use)


def test_a_text_keeps_its_link_name_once_listed_or_on_its_way_and_says_what_is_held():
    # RG-M2r 10 and RG-M2r2a 2: the words follow what is held, never
    # "submitted" for an entry the server has no receipt for.
    from dataclasses import replace
    from nostr.standup.drafting import ArticleDraft, Choice
    from nostr.standup.link_names import LinkNameLock
    from nostr.ui.standup_submit import RoundAnswer
    # Another's text this account takes part in is not its own.
    taking_part = replace(_entry(), sid="ee" * 32, role="contributor", d="ihr-text",
                          status_key=None)
    stored = replace(_entry(2), d="im-eingang", stored=True)
    submissions = FakeList([_entry(), taking_part, stored])
    submissions.loaded = True
    writer = _writer(submissions, FakeApi())
    other = SimpleNamespace(user_pubkey="b3" * 32)
    assert _lock(writer, in_use=False) == LinkNameLock()       # no account in use yet
    writer.account(PROFILE)
    assert _lock(writer) == LinkNameLock(held=Held.NOTHING)
    assert _lock(writer, "im-eingang") == LinkNameLock(held=Held.STORED)
    assert not _lock(writer, "anders").locked and not _lock(writer, "").locked
    assert not _lock(writer, "ihr-text").locked
    assert submissions.reads == 0
    draft = ArticleDraft(d="neu", title="T", summary="", content="x")
    run = writer.submit(PROFILE, draft, Choice(publication=PUBLICATION, name="M",
                                               signer="writer", rights=True), RoundAnswer(1))
    assert _lock(writer, "neu") == LinkNameLock(held=run.progress.held)
    assert _lock(writer, "ein-jahr", profile=other, in_use=False) == LinkNameLock()


def test_a_link_name_is_never_free_on_a_list_not_read_whole():
    # RG-M2r2a 1: after a restart nothing was read; the list is read once,
    # the name read-only and saying so until it answers.
    from nostr.standup.link_names import LinkNameLock
    submissions = FakeList([_entry()])
    writer = _writer(submissions, FakeApi())
    heard = []
    writer.listed.connect(heard.append)
    assert _lock(writer) == LinkNameLock(checking=True)
    assert _lock(writer, "anders") == LinkNameLock(checking=True)
    QApplication.processEvents()
    assert submissions.reads == 1 and heard == [WRITER]
    assert _lock(writer) == LinkNameLock(held=Held.NOTHING)
    assert _lock(writer, "anders") == LinkNameLock()            # read whole: never submitted


def test_without_a_server_the_list_is_never_read_for_a_link_name(tmp_path):
    # RG-M2r3 17: with no STANDUP server nothing is submitted from here,
    # yet Article Details read the writer's list (a signer prompt for
    # each page) for every text shown. Only this computer's mark tells.
    from nostr.standup.link_names import LinkNameLock, SubmittedMarks
    submissions = FakeList([_entry()])
    marks = SubmittedMarks(tmp_path / "marks.json")
    writer = _writer(submissions, FakeApi(), marks=marks)
    writer._server = ""
    assert _lock(writer) == LinkNameLock() and _lock(writer, "anders") == LinkNameLock()
    writer.check_link_name(PROFILE)
    QApplication.processEvents()
    assert submissions.reads == 0
    marks.remember(WRITER, "ein-jahr", stored=True)
    assert _lock(writer) == LinkNameLock(held=Held.STORED)
    assert submissions.reads == 0


@pytest.mark.parametrize("readable, complete, reads", [(False, True, 1), (True, False, 4)],
                         ids=["unreadable", "in part, read again quietly"])
def test_a_list_that_could_not_tell_keeps_the_link_name_and_check_again_reads_it(
        readable, complete, reads, later):
    from nostr.standup.link_names import LinkNameLock
    submissions = FakeList([], readable=readable, complete=complete)
    writer = _writer(submissions, FakeApi())
    _lock(writer, "anders")
    QApplication.processEvents()
    later.run()
    assert _lock(writer, "anders") == LinkNameLock(unchecked=True)
    assert submissions.reads == reads                           # then not by itself
    submissions._readable, submissions.complete = True, True
    writer.check_link_name(PROFILE)
    assert _lock(writer, "anders") == LinkNameLock(checking=True)
    QApplication.processEvents()
    assert submissions.reads == reads + 1 and _lock(writer, "anders") == LinkNameLock()


def test_a_link_name_check_whose_page_came_on_a_quiet_try_is_free(later):
    # Lessons 36: a page that did not come once says nothing; read again
    # quietly, it is read whole and the name is free.
    from nostr.standup.link_names import LinkNameLock
    submissions = HealingList()
    writer = _writer(submissions, FakeApi())
    assert _lock(writer, "anders") == LinkNameLock(checking=True)
    QApplication.processEvents()
    assert _lock(writer, "anders") == LinkNameLock(checking=True)   # still, not "couldn't"
    later.run()
    QApplication.processEvents()
    assert submissions.reads == 2 and _lock(writer, "anders") == LinkNameLock()


def test_the_mark_holds_the_link_name_after_a_restart(tmp_path):
    # RG-M2r2a 1: a new writer (the app started again), nothing read.
    from nostr.standup.link_names import LinkNameLock, SubmittedMarks
    from nostr.list_results import PublishResult, PublishStatus
    from nostr.standup.entries import RecordResult, RecordStatus
    marks = SubmittedMarks(tmp_path / "marks.json")
    writer = _writer(FakeList([]), FakeApi(), marks=marks)
    ports = writer._ports(writer.account(PROFILE), PUBLICATION)
    results = []
    writer.account(PROFILE).submissions.record = lambda entry, on_done: on_done(
        RecordResult(RecordStatus.STORED, entry, PublishResult(PublishStatus.SENT)))
    ports.record(_entry(), results.append)
    assert [r.status for r in results] == [RecordStatus.STORED]
    again = _writer(FakeList([], readable=False), FakeApi(),
                    marks=SubmittedMarks(tmp_path / "marks.json"))
    assert _lock(again, in_use=False) == LinkNameLock(held=Held.NOTHING)
    ports.mark_stored(_entry())
    again = _writer(FakeList([], readable=False), FakeApi(),
                    marks=SubmittedMarks(tmp_path / "marks.json"))
    assert _lock(again, in_use=False) == LinkNameLock(held=Held.STORED)


def test_an_entry_not_recorded_leaves_no_mark(tmp_path):
    from nostr.standup.link_names import SubmittedMarks
    from nostr.list_results import PublishResult, PublishStatus
    from nostr.standup.entries import RecordResult, RecordStatus
    marks = SubmittedMarks(tmp_path / "marks.json")
    writer = _writer(FakeList([]), FakeApi(), marks=marks)
    writer.account(PROFILE).submissions.record = lambda entry, on_done: on_done(
        RecordResult(RecordStatus.NOT_STORED, entry, PublishResult(PublishStatus.FAILED)))
    writer._ports(writer.account(PROFILE), PUBLICATION).record(_entry(), lambda _r: None)
    assert marks.get(WRITER, "ein-jahr") is None


def test_a_read_of_the_list_marks_each_writer_entry(tmp_path):
    from dataclasses import replace
    from nostr.standup.link_names import SubmittedMarks
    marks = SubmittedMarks(tmp_path / "marks.json")
    stored = replace(_entry(2), d="im-eingang", stored=True)
    writer = _writer(FakeList([_entry(), stored]), FakeApi(), marks=marks)
    _lock(writer, "anders")
    QApplication.processEvents()
    assert (marks.get(WRITER, "ein-jahr"), marks.get(WRITER, "im-eingang")) == (False, True)


@pytest.mark.parametrize("left", [False, True], ids=["none left", "another left"])
def test_a_discarded_entry_frees_the_link_name_once_none_is_left(tmp_path, left):
    from dataclasses import replace
    from nostr.standup.link_names import SubmittedMarks
    marks = SubmittedMarks(tmp_path / "marks.json")
    marks.remember(WRITER, "ein-jahr", stored=False)
    other = replace(_entry(2), stored=True)
    submissions = FakeList([_entry(1)] + ([other] if left else []))
    submissions.loaded = True                       # the sheet read it

    def forget(entry, on_done):
        submissions._entries.remove(entry)
        on_done(SimpleNamespace(stored=True))

    submissions.forget = forget
    writer = _writer(submissions, FakeApi(), marks=marks)
    writer.discard(PROFILE, _entry(1), lambda _stored: None)
    assert (marks.get(WRITER, "ein-jahr") is not None) is left
    assert _lock(writer).locked is left


def test_reading_the_list_says_so_for_article_details():
    writer = _writer(FakeList([_entry()]), FakeApi())
    heard = []
    writer.listed.connect(heard.append)
    _answer(writer)
    assert heard == [WRITER]


def test_a_sign_out_hears_when_what_was_read_is_written():
    # RG-M2r 8: the window forgets the signer only then.
    writer, profile = _wired_writer()
    said = []
    writer.stop(on_written=lambda: said.append("written"), on_done=lambda: said.append("done"))
    assert said == ["written", "done"]              # no account: at once
    account, said, waiting = writer.account(profile), [], []
    account.read_state.before_sign_out = waiting.append
    writer.stop(on_written=lambda: said.append("written"))
    assert said == [] and len(waiting) == 1
    waiting[0](None)                                # written, or its wait passed
    assert said == ["written"]
    QApplication.processEvents()                    # the session's answer, while it lives


def test_the_clients_of_other_servers_are_made_once_and_let_go_with_their_account():
    # Lesson 31: a client of another server refuses once its account stopped.
    from nostr.standup.errors import Code
    writer, profile = _wired_writer()
    account = writer.account(profile)
    assert account.client_for("https://standup.example") is account.api
    other = account.client_for("https://other.example")
    assert other is not account.api and account.client_for("https://other.example") is other
    writer.stop()
    refused = []
    other.lookup([("cc" * 32, 0)], print, refused.append)
    QApplication.processEvents()
    assert [error.code for error in refused] == [Code.STOPPED]


def test_an_entry_a_lookup_finds_is_marked_stored_on_the_list():
    # 13 at 72d5ddc: a lookup's receipt is a receipt held for that sid
    # with that status key. One the server has no receipt for is not.
    submissions = FakeList([_entry(1), _entry(2)])
    assert _round(_writer(submissions, FakeApi({1: "declined", 2: None}))) == (2, "")
    assert submissions.marked == [_entry(1)]
    assert [e.stored for e in submissions.entries()] == [True, False]


def _reread(submissions, entry):
    writer = _writer(submissions, FakeApi())
    said = []
    writer._ports(writer.account(SimpleNamespace(user_pubkey=WRITER)), PUBLICATION).reload(
        entry, said.append)
    QApplication.processEvents()
    LATER.run()
    assert len(said) == 1
    return said[0]


def test_after_a_stale_revision_the_list_is_read_again_for_that_devices_entry():
    # Contract 2 at 72d5ddc: another device stored this round first. The
    # list is read again; only that device's stored entry (other keys)
    # is the one waited for, never this device's own entry.
    from dataclasses import replace
    from nostr.standup.ports import Reread
    mine = _entry(1)
    theirs = replace(mine, s_sub="d4" * 32, status_key="ab" * 32, stored=True, updated_at=0)
    submissions = FakeList([mine])
    assert _reread(submissions, mine) == Reread(False)
    submissions.arriving = [replace(theirs, stored=False)]      # not stored there yet
    assert _reread(submissions, mine) == Reread(False)
    submissions.arriving = [theirs]                              # older, but stored
    assert _reread(submissions, mine) == Reread(True)
    assert submissions.reads == 3 and submissions.entry(mine.sid) == theirs


def test_that_devices_entry_whose_pictures_are_missing_is_never_said_to_be_submitted():
    # Contract 12.4 at 33d92c9: "Submitted on another device." only while
    # its receipt misses no picture; this app has none of its files.
    from dataclasses import replace
    from nostr.standup.ports import Reread
    mine = _entry(1)
    # Other keys (another s_sub); its status key is the one the fake knows.
    theirs = replace(mine, s_sub="d4" * 32, stored=True, updated_at=0)
    submissions = FakeList([mine])
    submissions.arriving = [theirs]
    writer = _writer(submissions, FakeApi({1: "submitted"}, missing=("ef" * 32,)))
    said = []
    writer._ports(writer.account(PROFILE), PUBLICATION).reload(mine, said.append)
    QApplication.processEvents()
    LATER.run()
    assert said == [Reread(True, pictures_missing=True)]
    # Its pictures all there, or the server not reached (asked again
    # quietly first): arrived, as the list says.
    for api in (FakeApi({1: "submitted"}), FakeApi(fails=True)):
        submissions.arriving = [theirs]
        writer = _writer(submissions, api)
        said = []
        writer._ports(writer.account(PROFILE), PUBLICATION).reload(mine, said.append)
        QApplication.processEvents()
        LATER.run()
        assert said == [Reread(True)]
        assert len(api.asked) == (1 if not api.fails else 1 + len(failures.QUIET_RETRY_MS))


def test_a_list_that_cannot_be_read_again_says_why_and_one_read_in_part_says_nothing():
    # Lessons 36: a page that did not come is partial, read again quietly;
    # nothing about parts is said, and Reload stays.
    from nostr.standup.lists import SUBMISSIONS_UNREADABLE
    answer = _reread(FakeList([_entry(1)], readable=False), _entry(1))
    assert not answer.arrived and answer.said == i18n._(SUBMISSIONS_UNREADABLE)
    submissions = FakeList([_entry(1)], complete=False)
    answer = _reread(submissions, _entry(1))
    assert not answer.arrived and answer.said == "" and submissions.reads == 4


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


@pytest.mark.parametrize("submissions, api, said, from_list", [
    (FakeList([_entry()]), FakeApi(fails=True), failures.CHECK_CONNECTION, False),  # no answer
    (FakeList([_entry()]), FakeApi(unreadable=True), ROUND_UNKNOWN, False),   # has something
    (FakeList([_entry()]), FakeApi("declined", other_sid=True), ROUND_UNKNOWN, False),
    (FakeList(readable=False), FakeApi(), SUBMISSIONS_UNREADABLE, True),
    (FakeList(complete=False), FakeApi(), failures.CHECK_CONNECTION, True),  # a page missing
], ids=["no answer", "unreadable", "another submission", "list unreadable", "list incomplete"])
def test_a_round_that_cannot_be_told_is_not_guessed(submissions, api, said, from_list):
    # RG-M2b 2: never "already submitted", never round 1 by default.
    # RG-M2r 4: what the list could not tell is said as the list's, so Try
    # Again reads it again; what the server could not, asks it again.
    # Lessons 36: the connection or a page that did not come, only once
    # the quiet tries are spent.
    answer = _answer(_writer(submissions, api))
    assert (answer.round, answer.words) == (None, said)
    assert answer.from_list is from_list


class HealingList(FakeList):
    """Read in part once (a page had not arrived), whole from then on."""

    def load(self, on_done) -> None:
        self.reads += 1
        self.loaded = True
        self.complete = self.reads > 1
        on_done(ListRead(ReadStatus.INCOMPLETE if self.reads == 1 else ReadStatus.COMPLETE))


def test_a_list_read_in_part_is_read_again_quietly_and_says_nothing(later):
    # RG-M2r 4 (the reviewer's probe): read once in part, it was never
    # read again in the session. Lessons 36: a page that did not come is
    # read again quietly, after a wait, and nothing is said meanwhile.
    submissions = HealingList()
    writer = _writer(submissions, FakeApi())
    answers = []
    writer.round_for(SimpleNamespace(user_pubkey=WRITER), PUBLICATION, "ein-jahr",
                     answers.append)
    QApplication.processEvents()
    assert answers == [] and submissions.reads == 1             # waiting, nothing said
    assert later.run() == [failures.QUIET_RETRY_MS[0]]
    assert submissions.reads == 2 and [(a.round, a.words) for a in answers] == [(1, "")]
    assert _round(writer) == (1, "") and submissions.reads == 2  # whole: not read again


def test_the_publications_wait_for_a_list_read_in_part_to_be_read_again_quietly(later):
    # Lessons 36: the sheet hears the publications once, with the list
    # read whole on a quiet try, never the read in part.
    submissions = HealingList()
    writer = _writer(submissions, FakeApi(),
                     destinations=SimpleNamespace(find=lambda found, _failed: found([])))
    got, failed = _load(writer)
    assert (got, failed, submissions.reads) == ([], [], 1)
    assert later.run() == [failures.QUIET_RETRY_MS[0]]
    assert got == [[]] and submissions.reads == 2
    assert writer.account(PROFILE).list_problem == ""


@pytest.mark.parametrize("submissions", [FakeList(readable=False),
                                         FakeList(complete=False)],
                         ids=["unreadable", "in part, the quiet tries spent"])
def test_try_again_on_a_round_the_list_could_not_tell_reads_it_again(submissions):
    # One read, or one and its quiet tries, per Try Again.
    tries = 1 if submissions.complete else 1 + len(failures.QUIET_RETRY_MS)
    writer = _writer(submissions, FakeApi())
    assert _answer(writer).from_list and submissions.reads == tries
    assert _answer(writer).from_list and submissions.reads == tries  # said again, not read
    writer.reload(SimpleNamespace(user_pubkey=WRITER))
    _answer(writer)
    assert submissions.reads == 2 * tries
    _answer(writer)
    assert submissions.reads == 2 * tries


def test_a_list_that_could_not_be_read_is_said_again_without_asking_again():
    submissions = FakeList(readable=False)
    writer = _writer(submissions, FakeApi())
    assert _round(writer) == _round(writer) == (None, SUBMISSIONS_UNREADABLE)
    assert submissions.reads == 1


class Publications:
    """The publications' finder: refuses with ``error`` the first
    ``refusals`` times (-1: always), then lists none."""

    def __init__(self, error, refusals=-1) -> None:
        self.error, self.refusals, self.asked = error, refusals, 0

    def find(self, found, failed) -> None:
        self.asked += 1
        if self.refusals != 0:
            self.refusals -= 1
            failed(self.error)
        else:
            found([])


def _load(writer):
    got, failed = [], []
    writer.destinations(PROFILE, lambda _d, used: got.append(used), failed.append)
    QApplication.processEvents()
    return got, failed


@pytest.mark.parametrize("code", [Code.OFFLINE, Code.TIMEOUT, Code.SERVER])
def test_publications_the_server_did_not_give_once_are_asked_again_quietly(later, code):
    # Lessons 36: nothing is said for a try the connection stopped.
    publications = Publications(StandupError(code), refusals=1)
    writer = _writer(FakeList(), FakeApi(), destinations=publications)
    got, failed = _load(writer)
    assert (got, failed) == ([], [])                # waiting, nothing said
    assert later.run() == [failures.QUIET_RETRY_MS[0]]
    assert publications.asked == 2 and got == [[]] and failed == []


def test_publications_that_never_come_are_said_once_when_the_tries_are_spent(later):
    publications = Publications(StandupError(Code.OFFLINE))
    writer = _writer(FakeList(), FakeApi(), destinations=publications)
    got, failed = _load(writer)
    assert later.run() == list(failures.QUIET_RETRY_MS)
    assert got == [] and [error.code for error in failed] == [Code.OFFLINE]
    assert publications.asked == 1 + len(failures.QUIET_RETRY_MS)


def test_publications_refused_otherwise_are_said_at_once(later):
    publications = Publications(StandupError(Code.RATE_LIMITED, retry_after=60))
    writer = _writer(FakeList(), FakeApi(), destinations=publications)
    got, failed = _load(writer)
    assert later.waiting == [] and len(failed) == 1 and publications.asked == 1


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
    """A writer whose account has a run at each of ``progresses``, each
    ended by SubmissionRun's own rule."""
    from nostr.ui.standup_submit import SubmissionRun
    writer = _writer(FakeList(), FakeApi(), media_keys=SimpleNamespace(clear=lambda: None),
                     read_state=ReadStateList([]), destinations=SimpleNamespace(stop=lambda: None))
    account = writer.account(SimpleNamespace(user_pubkey=WRITER))
    for progress in progresses:
        account.runs.append(SimpleNamespace(
            submitting=SimpleNamespace(stop=lambda: None), progress=progress,
            finished=SubmissionRun.finished.fget(SimpleNamespace(progress=progress, _let_go=False)),
            choice=SimpleNamespace(name="Einundzwanzig Magazin")))
    return writer


@pytest.mark.parametrize("progresses, said", [
    ((Progress(Step.UPLOADING),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before "
     "anything was sent."),
    ((Progress(Step.SIGNING),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before "
     "anything was sent."),
    ((Progress(Step.SENDING, sent=True, held=Held.MAYBE),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before the "
     "server confirmed it: open Submit to a Publication with that account to see whether it "
     "arrived."),
    ((Progress(Step.CHECKING, sent=True, held=Held.MAYBE),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before the "
     "server confirmed it: open Submit to a Publication with that account to see whether it "
     "arrived."),
    ((Progress(Step.REUPLOADING, sent=True, held=Held.STORED),),
     "Your submission to Einundzwanzig Magazin is in, but a picture was still being uploaded "
     "when the account changed."),
    # RG-M2r 2: stored, then signing the same body again for a picture
    # the receipt missed: in, never "before the server confirmed it".
    ((Progress(Step.SIGNING, sent=True, held=Held.STORED),),
     "Your submission to Einundzwanzig Magazin is in, but a picture was still being uploaded "
     "when the account changed."),
    # A request refused for sure, signed again: nothing was sent.
    ((Progress(Step.SIGNING, sent=True, held=Held.NOTHING),),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before "
     "anything was sent."),
    ((Progress(Step.SUBMITTED, sent=True, held=Held.STORED, message=MessageStatus.SENDING),),
     "Your submission to Einundzwanzig Magazin is in, but your message may not have reached "
     "the editorial team: the account changed."),
    ((Progress(Step.SUBMITTED, sent=True, held=Held.STORED, message=MessageStatus.SIGNING),),
     "Your submission to Einundzwanzig Magazin is in, but your message may not have reached "
     "the editorial team: the account changed."),
    ((Progress(Step.SUBMITTED, sent=True, held=Held.STORED, message=MessageStatus.FAILED),),
     None),                                          # said when it failed
    ((Progress(Step.UPLOADING), Progress(Step.SENDING, sent=True, held=Held.MAYBE)),
     "Your submission to Einundzwanzig Magazin stopped when the account changed, before the "
     "server confirmed it: open Submit to a Publication with that account to see whether it "
     "arrived. 1 other submission stopped as well."),
    ((Progress(Step.SUBMITTED, sent=True, held=Held.STORED), Progress(Step.FAILED)), None),
], ids=["uploading", "signing", "sending", "checking", "pictures", "stored re-signing",
        "refused re-signing", "message", "message signing", "message failed", "two", "none"])
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


def test_a_real_run_stopped_while_its_missing_picture_goes_up_is_said_to_be_in():
    # RG-M2r 2 (the reviewer's probe, through a real run): stored, the
    # run signs the same body again for the picture the receipt missed.
    from tests.standup_vectors import sk
    from tests.test_standup_submitting import HeldSigner, World as SubmittingWorld

    world = SubmittingWorld(signer=HeldSigner(sk("writer")))
    world.uploads.lose = 1
    world.submitting.start()
    world.settle()
    world.signer.release()
    world.settle()
    progress = world.submitting.progress
    assert progress.step is Step.SIGNING and progress.receipt is None
    run = SimpleNamespace(progress=progress, choice=SimpleNamespace(name="Magazin"))
    assert standup_run.stopped_words([run]) == (
        "Your submission to Magazin is in, but a picture was still being uploaded when the "
        "account changed.")


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
    """A run's signal and words; its watcher behind the sheet is the real one."""

    changed = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.choice = SimpleNamespace(name="Einundzwanzig Magazin")
        self.finished = False
        self.progress = Progress(Step.SIGNING)
        self._behind = None

    def behind_the_sheet(self, on_change) -> None:
        from nostr.ui.standup_submit import SubmissionRun
        SubmissionRun.behind_the_sheet(self, on_change)


def test_a_sheet_closed_twice_on_a_run_says_each_change_once_and_none_while_it_shows_it():
    # One watcher per run: closing the sheet again did not connect a
    # second, and a sheet that shows the run again says it itself.
    writer, run, said = _writer(FakeList(), FakeApi()), Run(), []
    writer.status.connect(said.append)
    writer.in_background(run)
    run.behind_the_sheet(None)                       # shown again
    run.changed.emit(Progress(Step.SENDING, sent=True))
    assert said == ["Submitting …"]
    writer.in_background(run)
    run.changed.emit(Progress(Step.SUBMITTED, sent=True, receipt=object()))
    assert said == ["Submitting …", "Submitting …",
                    "Submitted. In the inbox of Einundzwanzig Magazin."]


def test_in_the_background_the_status_bar_says_submitted_only_once_it_is_in(german):
    writer, run, said = _writer(FakeList(), FakeApi()), Run(), []
    writer.status.connect(said.append)
    writer.in_background(run)
    run.changed.emit(Progress(Step.SENDING))
    run.changed.emit(Progress(Step.CHECKING))
    assert said == ["Wird eingereicht …"]
    run.changed.emit(Progress(Step.SUBMITTED))
    assert said[-1] == "Eingereicht. Im Eingang von Einundzwanzig Magazin."


def test_in_the_background_the_first_message_says_why_the_signer_asks_and_when_it_failed():
    # RG-M2b 11: the prompt came out of context, and a failure was lost.
    writer, run, said = _writer(FakeList(), FakeApi()), Run(), []
    writer.status.connect(said.append)
    writer.in_background(run)
    run.changed.emit(Progress(Step.SUBMITTED, message=MessageStatus.SIGNING))
    assert said[-1] == ("Your submission to Einundzwanzig Magazin is in. Confirm your message "
                        "to the editorial team in your signer app.")
    run.changed.emit(Progress(Step.SUBMITTED, message=MessageStatus.SENDING))
    run.changed.emit(Progress(Step.SUBMITTED, message=MessageStatus.FAILED,
                              message_failure=Failure(words.SERVER_TROUBLE, Step.SUBMITTED,
                                                      title=words.MESSAGE_NOT_SENT)))
    assert said[-1] == ("Your message to the editorial team of Einundzwanzig Magazin wasn’t "
                        "sent: open Submit to a Publication for this text to send it again.")
    # One the server may hold is never said not to be sent (RG-M2r3 7).
    run.changed.emit(Progress(Step.SUBMITTED, message=MessageStatus.FAILED,
                              message_failure=Failure(words.MESSAGE_MAYBE_ARRIVED,
                                                      Step.SUBMITTED,
                                                      title=words.MESSAGE_NOT_CONFIRMED)))
    assert said[-1] == ("Your message to the editorial team of Einundzwanzig Magazin may not "
                        "have arrived: open Submit to a Publication for this text before you "
                        "send it again.")
    run.changed.emit(Progress(Step.SUBMITTED, message=MessageStatus.SENT))
    assert said[-1] == "Submitted. In the inbox of Einundzwanzig Magazin."
    assert all(a != b for a, b in zip(said, said[1:]))  # a change is said once


@pytest.mark.parametrize("progress, finished", [
    (Progress(Step.SUBMITTED), True),
    (Progress(Step.SUBMITTED, message=MessageStatus.SENT), True),
    (Progress(Step.SUBMITTED, message=MessageStatus.SIGNING), False),
    (Progress(Step.SUBMITTED, message=MessageStatus.SENDING), False),
    (Progress(Step.SUBMITTED, message=MessageStatus.FAILED), False),   # Send Again there
    (Progress(Step.FAILED), True),
    (Progress(Step.CANCELLED), True),
    (Progress(Step.CHECKING, sent=True), False),
])
def test_a_run_ends_with_its_first_message(progress, finished):
    # Opened again for its work, the sheet shows a run whose message did
    # not go, with Send Again, instead of a new form.
    from nostr.ui.standup_submit import SubmissionRun
    def ended(let_go: bool) -> bool:
        return SubmissionRun.finished.fget(SimpleNamespace(progress=progress, _let_go=let_go))

    assert ended(False) is finished
    # Let go once a sheet showed it with Send Again: ended, if it ended.
    assert ended(True) is (progress.phase.name in ("DONE", "FAILED", "CANCELLED"))


@pytest.mark.parametrize("retry", [Step.REUPLOADING, Step.SIGNING, None])
def test_a_run_whose_pictures_did_not_arrive_holds_their_files_and_is_shown_again(retry):
    # Contract 12.4 at 33d92c9: only the app holding the files uploads the
    # missing ones; once the sheet closed, Try Again must still be there,
    # not the words for an app that holds none of them.
    from nostr.standup.submitting import Failure
    from nostr.ui.standup_submit import SubmissionRun
    progress = Progress(Step.FAILED, held=Held.STORED, failure=Failure(
        words.PICTURE_MISSING, retry, title=words.PICTURE_NOT_IN))
    for let_go in (False, True):
        run = SimpleNamespace(progress=progress, _let_go=let_go)
        assert SubmissionRun.finished.fget(run) is (retry is not Step.REUPLOADING)


def test_in_the_background_a_failure_is_said_in_its_words(german):
    from nostr.standup.submitting import Failure
    writer, run, said = _writer(FakeList(), FakeApi()), Run(), []
    writer.status.connect(said.append)
    writer.in_background(run)
    run.changed.emit(Progress(Step.FAILED, failure=Failure(
        failures.CHECK_CONNECTION, Step.SIGNING, title=words.NOT_SUBMITTED)))
    assert said[-1] == ("Dein Artikel wurde nicht eingereicht. Prüfe deine Internetverbindung "
                        "und versuche es erneut.")


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
    sealed = _sealed(nostr_crypto.get_public_key(s_sub).hex(), s_sub, sid)
    signer, sent, said = SignAs(sk("writer")), [], []

    def send_message(submission_id, wrap, *, kind_hint, on_success, on_failure):
        sent.append((submission_id, wrap, kind_hint))
        on_success(7)                               # only a seq says it is there

    account = SimpleNamespace(
        writer=pk("writer"), api=SimpleNamespace(send_message=send_message), stopped=False,
        signer=SimpleNamespace(sign_event=lambda unsigned, *, on_success, on_failure:
                               signer(unsigned, on_success, on_failure)))
    standup_message.send_first_message(account, sealed, "Passt zu eurer Reihe.",
                                lambda: said.append("signed"), lambda: said.append("sent"),
                                lambda: said.append("not sent"), later=LATER)
    (submission_id, wrap, kind_hint), = sent
    opened = conversation.open_item(wrap, s_sub, sid)
    assert (submission_id, kind_hint, said) == (sid, "message", ["signed", "sent"])
    assert opened.sender == pk("writer")
    assert opened.rumor["content"] == "Passt zu eurer Reihe."
    assert len(signer.requests) == 1                # one prompt: the seal


@pytest.mark.parametrize("error, wait", [
    (StandupError(Code.RATE_LIMITED, retry_after=45), 45),     # a limit: its wait goes on
    (StandupError(Code.RATE_LIMITED), None),
    (StandupError(Code.SERVER, retry_after=45), None),         # not a limit: no wait
])
def test_a_message_refused_for_a_limit_says_how_long_to_wait(error, wait):
    from nostr import crypto as nostr_crypto
    from tests.standup_fakes import SignAs
    from tests.standup_vectors import pk, sk

    s_sub = bytes(range(1, 33))
    sealed = _sealed(nostr_crypto.get_public_key(s_sub).hex(), s_sub, "ab" * 32)
    signer, waits = SignAs(sk("writer")), []

    def send_message(submission_id, wrap, *, kind_hint, on_success, on_failure):
        on_failure(error)

    account = SimpleNamespace(
        writer=pk("writer"), api=SimpleNamespace(send_message=send_message), stopped=False,
        signer=SimpleNamespace(sign_event=lambda unsigned, *, on_success, on_failure:
                               signer(unsigned, on_success, on_failure)))
    standup_message.send_first_message(account, sealed, "Hallo", lambda: None, lambda: None,
                                lambda error: waits.append(error), later=LATER)
    LATER.run()                                     # a 5xx is asked again quietly first
    assert waits == [error]                         # with its wait, read by the run


def _message_account(answers):
    """An account whose server answers each message POST with the next of
    ``answers`` (a StandupError, or a seq)."""
    from nostr import crypto as nostr_crypto
    from tests.standup_fakes import SignAs
    from tests.standup_vectors import pk, sk

    s_sub = bytes(range(1, 33))
    sealed = _sealed(nostr_crypto.get_public_key(s_sub).hex(), s_sub, "ab" * 32)
    signer, posted = SignAs(sk("writer")), []

    def send_message(submission_id, wrap, *, kind_hint, on_success, on_failure):
        posted.append(wrap)
        answer = answers.pop(0)
        on_failure(answer) if isinstance(answer, StandupError) else on_success(answer)

    account = SimpleNamespace(
        writer=pk("writer"), api=SimpleNamespace(send_message=send_message), stopped=False,
        signer=SimpleNamespace(sign_event=lambda unsigned, *, on_success, on_failure:
                               signer(unsigned, on_success, on_failure)))
    return account, sealed, signer, posted


@pytest.mark.parametrize("code", [Code.OFFLINE, Code.TIMEOUT, Code.SERVER])
def test_a_message_the_connection_stopped_is_sent_again_quietly_with_no_new_prompt(code):
    # Lessons 36: the submission is in; the message goes again after a
    # wait, the same wrap, and nothing is said meanwhile.
    account, sealed, signer, posted = _message_account([StandupError(code), 7])
    said = []
    standup_message.send_first_message(
        account, sealed, "Hallo", lambda: said.append("signed"), lambda: said.append("sent"),
        lambda error: said.append("not sent"), later=LATER)
    assert said == ["signed"] and len(posted) == 1
    assert LATER.run() == [failures.QUIET_RETRY_MS[0]]
    assert said == ["signed", "sent"] and len(posted) == 2 and posted[0] == posted[1]
    assert len(signer.requests) == 1


def test_a_message_that_never_gets_through_is_said_not_sent_once():
    account, sealed, _signer, posted = _message_account(
        [StandupError(Code.OFFLINE) for _ in range(1 + len(failures.QUIET_RETRY_MS))])
    said = []
    standup_message.send_first_message(
        account, sealed, "Hallo", lambda: None, lambda: said.append("sent"),
        lambda error: said.append("not sent"), later=LATER)
    assert LATER.run() == list(failures.QUIET_RETRY_MS)
    assert said == ["not sent"] and len(posted) == 1 + len(failures.QUIET_RETRY_MS)


def test_a_message_that_may_have_arrived_is_never_sent_twice():
    # No idempotency for messages in the contract: a second POST could be
    # a second message.
    account, sealed, _signer, posted = _message_account(
        [StandupError(Code.TIMEOUT, maybe_stored=True)])
    said = []
    standup_message.send_first_message(
        account, sealed, "Hallo", lambda: None, lambda: said.append("sent"),
        lambda error: said.append("not sent"), later=LATER)
    assert LATER.waiting == [] and said == ["not sent"] and len(posted) == 1


def test_a_message_with_a_secret_key_in_it_is_never_sent():
    from nostr.bech32 import encode_nsec
    from tests.standup_vectors import pk, sk

    s_sub = bytes(range(1, 33))
    sent, said = [], []
    account = SimpleNamespace(writer=pk("writer"), api=SimpleNamespace(
        send_message=lambda *a, **k: sent.append(a)), signer=None, stopped=False)
    sealed = _sealed("00" * 32, s_sub, "ab" * 32)
    nsec = encode_nsec(sk("writer").hex())
    standup_message.send_first_message(account, sealed, f"mein Schlüssel {nsec}",
                                lambda: said.append("signed"), lambda: said.append("sent"),
                                lambda error: said.append(error.code), later=LATER)
    QApplication.processEvents()
    assert sent == [] and said == [Code.REFUSED]        # built here: not the signer's


def test_a_message_this_app_refuses_to_seal_is_not_said_as_the_signers_no():
    # RG-M2r3 7: a room key that is not the room's is refused here, before
    # the signer app is asked.
    from tests.standup_fakes import SignAs
    from tests.standup_vectors import pk, sk
    signer, said = SignAs(sk("writer")), []
    account = SimpleNamespace(
        writer=pk("writer"), api=SimpleNamespace(send_message=lambda *a, **k: None),
        stopped=False, signer=SimpleNamespace(sign_event=lambda unsigned, *, on_success,
                                              on_failure: signer(unsigned, on_success,
                                                                 on_failure)))
    sealed = _sealed(pk("editor"), bytes(range(1, 33)), "ab" * 32)     # a valid key
    standup_message.send_first_message(account, sealed, "Hallo", lambda: None, lambda: None,
                                       lambda error: said.append(error.code), later=LATER)
    QApplication.processEvents()
    assert said == [Code.REFUSED] and signer.requests == []


def _sealed(room: str, s_sub: bytes, sid: str) -> SimpleNamespace:
    """What send_first_message reads of a Sealed."""
    return SimpleNamespace(body={"p_sub": room}, p_sub=room, s_sub=s_sub, submission_id=sid)


def _wired_writer():
    from nostr.profiles import Profile
    from tests.outbox_fakes import FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool

    writer = StandupWriter(session_pool=FakeSessionPool(FakeClient()), relay_pool=FakePool(),
                           relay_directory=FakeRelayDirectory(), media_store=Store(),
                           server="https://standup.example")
    profile = Profile(user_pubkey=WRITER, bunker_pubkey="b" * 64,
                      bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64)
    return writer, profile


@pytest.mark.parametrize("deleted", [False, True], ids=["let go", "deleted"])
def test_a_first_message_sealed_after_the_account_was_let_go_is_never_posted(monkeypatch,
                                                                              deleted):
    # RG-M2r 3, lesson 31: the person confirms the seal on the phone after
    # switching account; nothing is posted, no session opened for the
    # account left, and nothing touches what was deleted with it.
    from nostr.ui import standup_message as module

    pending = {}
    monkeypatch.setattr(module.conversation, "message_rumor", lambda **kw: {"rumor": kw})
    monkeypatch.setattr(module.conversation, "seal_and_wrap",
                        lambda signer, rumor, s_sub, room, on_success, on_failure,
                        on_refused: pending.update(on_success=on_success, on_failure=on_failure))
    writer, profile = _wired_writer()
    account = writer.account(profile)
    posted, said = [], []
    monkeypatch.setattr(account.api, "send_message", lambda *a, **k: posted.append(a))
    sealed = _sealed("ab" * 32, b"x", "cd" * 32)
    standup_message.send_first_message(account, sealed, "Hallo", lambda: said.append("signed"),
                                lambda: said.append("sent"), lambda _error: said.append("not sent"),
                                later=LATER)
    writer.stop()
    QApplication.processEvents()
    if deleted:
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    pending["on_success"]({"kind": 1059})            # the signer app answers late
    pending["on_failure"]("timeout")
    QApplication.processEvents()
    assert posted == [] and said == []


def test_no_late_answer_of_an_account_let_go_reaches_the_sheet_or_reads_its_list():
    # Lesson 31: the publications, the list read for them and for a round,
    # and a removal each answer after the account changed; none goes on.
    from nostr.list_results import ListRead, ReadStatus

    writer, profile = _wired_writer()
    account = writer.account(profile)
    later, loads, said = {}, [], []
    account.destinations.find = lambda found, failed: later.update(found=found, failed=failed)
    account.submissions.load = loads.append
    account.submissions.forget = lambda entry, done: later.update(forgot=done)
    writer.destinations(profile, lambda *a: said.append("publications"), said.append)
    writer.destinations(profile, lambda *a: said.append("publications"), said.append)
    later["found"]([])                              # the list is read for them
    writer.discard(profile, _entry(), lambda stored: said.append("discarded"))
    writer.round_for(profile, PUBLICATION, "ein-jahr", lambda answer: said.append("round"))
    QApplication.processEvents()
    assert len(loads) == 1                          # one read for both (RG-M2r3 13)
    writer.stop()
    QApplication.processEvents()
    later["found"]([])
    later["failed"](object())
    for read in loads:
        read(ListRead(ReadStatus.COMPLETE, value=None))
    later["forgot"](SimpleNamespace(stored=True))
    QApplication.processEvents()
    assert said == [] and len(loads) == 1


def test_one_read_of_the_list_is_on_its_way_at_a_time_and_every_asker_hears_it():
    # RG-M2r3 13: Article Details, the sheet's publications and a round
    # each started a read of their own, so the signer was asked for every
    # page three times. One read is on its way; each hears it once.
    from nostr.list_results import ListRead, ReadStatus
    from nostr.standup.link_names import LinkNameLock

    writer, profile = _wired_writer()
    account = writer.account(profile)
    loads, said = [], []
    account.destinations.find = lambda found, failed: found([])
    account.submissions.load = loads.append
    assert writer.link_name(profile, "ein-jahr", in_use=True) == LinkNameLock(checking=True)
    writer.destinations(profile, lambda *a: said.append("publications"), said.append)
    writer.round_for(profile, PUBLICATION, "anders", lambda answer: said.append("round"))
    QApplication.processEvents()
    assert len(loads) == 1 and said == []
    loads[0](ListRead(ReadStatus.UNREADABLE, words="nicht gelesen"))
    QApplication.processEvents()
    assert sorted(said) == ["publications", "round"]
    assert not account.checking and account.check_failed
    # It ended: the next asker reads anew (Check Again), one read again.
    writer.check_link_name(profile)
    writer.destinations(profile, lambda *a: said.append("publications"), said.append)
    QApplication.processEvents()
    assert len(loads) == 2


@pytest.mark.parametrize("when", ["deciding", "asking the server"])
def test_a_round_answered_after_the_account_was_let_go_is_never_said(monkeypatch, when):
    from nostr.standup import rounds as module

    asked = {}
    monkeypatch.setattr(module, "look_up_all",
                        lambda api, entries, found, failed, **_k: asked.update(found=found))
    writer, profile = _wired_writer()
    account = writer.account(profile)
    account.submissions.loaded = True
    account.submissions.entries = lambda: [_entry()]
    said = []
    writer.round_for(profile, PUBLICATION, "ein-jahr", said.append)
    if when == "asking the server":
        QApplication.processEvents()
        assert "found" in asked
    writer.stop()
    QApplication.processEvents()
    if when == "deciding":
        assert asked == {}                          # nothing asked for the account left
    else:
        asked["found"]({_entry().sid: None})
    QApplication.processEvents()
    assert said == []


def test_a_list_of_an_account_let_go_is_never_read_or_written_again():
    # Lesson 31: a late caller asks no signer for the account left; it is
    # refused on a later turn.
    from tests.test_standup_lists import entry

    writer, profile = _wired_writer()
    account = writer.account(profile)
    reached, answers = [], []
    account.submissions._list.relays = lambda *a, **k: reached.append("read")
    account.submissions._list.publish = lambda *a, **k: reached.append("write")
    writer.stop()
    account.submissions.load(lambda read: answers.append(read.readable))
    account.submissions.record(entry(), lambda result: answers.append(result.status.value))
    assert answers == []
    QApplication.processEvents()
    assert reached == [] and answers == [False, "not stored"]


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


def test_a_stale_refusal_is_held_and_no_round_is_offered_after_a_restart(tmp_path):
    # RG-M2r3 1, contract 2 at 0456312: a revision 1 refused as
    # stale_revision is another device's round. Held with its entry, so
    # neither a reopened sheet nor a restarted app offers it again as an
    # earlier try that was not sent: "Submitted on another device." with
    # Reload, until that device's entry arrived.
    from dataclasses import replace
    from nostr.standup import words
    from nostr.standup.link_names import SubmittedMarks
    mine = _entry(1)
    marks = SubmittedMarks(tmp_path / "marks.json")
    writer = _writer(FakeList([mine]), FakeApi(None), marks=marks)
    assert _answer(writer).unsent is mine                        # not refused yet
    ports = writer._ports(writer.account(PROFILE), PUBLICATION)
    assert not ports.refused(mine)
    ports.refuse(mine)
    # What a run asks before it builds anything (Submitting.start).
    assert ports.refused(mine) and not ports.refused(_entry(2))
    for again in (writer, _writer(FakeList([mine]), FakeApi(None),
                                  marks=SubmittedMarks(tmp_path / "marks.json"))):
        answer = _answer(again)
        assert (answer.round, answer.unsent, answer.elsewhere) == (None, None, mine)
        assert (answer.title, answer.words, answer.from_list) == (
            words.SUBMITTED_ELSEWHERE, i18n._(words.ELSEWHERE_NOT_HERE_YET), True)
    # That device's stored entry arrived: the sid shows it, and its round
    # is what the server says of it.
    theirs = replace(mine, s_sub="d4" * 32, status_key="c" * 62 + "01", stored=True)
    submissions = FakeList([mine])
    submissions.arriving = [theirs]
    api = FakeApi("submitted")
    answer = _answer(_writer(submissions, api, marks=SubmittedMarks(tmp_path / "marks.json")))
    assert api.asked == [[theirs.status_key]]
    assert (answer.round, answer.words, answer.unsent, answer.elsewhere) == (None, "", None, None)
