# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An upload the Media Library starts is a job the person can act on.

What must hold:

  Two files with the same name in flight at once are two jobs: each
  keeps its own progress and its own outcome.

  Cancel stops an upload while its bytes are on their way: the request
  is aborted, nothing is reported failed or finished, and a signer that
  answers late sends nothing. Once a server holds the file, the copies
  on the other servers finish in the background and Cancel is refused.

  A failed upload can be retried as it was, and dismissed.

  The name a file had when it was added is remembered, also across a
  restart, because media servers keep no names.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.blossom.client import BlossomClient  # noqa: E402
from nostr.blossom.names import MediaNames  # noqa: E402
from nostr.blossom.settings import BlossomSettings  # noqa: E402
from nostr.blossom.store import MediaStore  # noqa: E402
from tests.blossom_fakes import (  # noqa: E402
    BODY, MIRROR, SERVER, SHA, FakeBlossomServer, FakeNam, FakeProfile,
    FakeSessionPool, FakeSigner, error_reply,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class Jobs:
    def __init__(self, tmp_path, *, servers=(SERVER,), replies=None, defer=False):
        settings = BlossomSettings(tmp_path / "blossom_servers.json")
        settings.set_custom_servers(list(servers))
        self.server = FakeBlossomServer(SERVER)
        self.nam = FakeNam(replies, responder=self.server)
        self.signer = FakeSigner(defer=defer)
        self.names = MediaNames(tmp_path / "media_names.json")
        self.store = MediaStore(
            session_pool=FakeSessionPool(self.signer),
            profile_provider=FakeProfile,
            settings=settings,
            client=BlossomClient(nam=self.nam),
            names=self.names,
        )
        self.events = []
        s = self.store
        s.upload_started.connect(lambda n: self.events.append(("started", n)))
        s.upload_finished.connect(lambda n, m: self.events.append(("finished", n)))
        s.upload_failed.connect(lambda n, r: self.events.append(("failed", n)))
        s.upload_cancelled.connect(lambda n: self.events.append(("cancelled", n)))
        s.upload_progress.connect(lambda n, a, b: self.events.append(("progress", n, a)))

    def kinds(self, name):
        return [e[0] for e in self.events if e[1] == name]


def _file(tmp_path, name="photo.png", body=BODY):
    path = tmp_path / name
    path.write_bytes(body)
    return str(path)


def test_two_files_of_one_name_are_two_jobs(tmp_path):
    jobs = Jobs(tmp_path, defer=True)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = jobs.store.upload_file(_file(tmp_path / "a"))
    second = jobs.store.upload_file(_file(tmp_path / "b", body=b"other bytes"))
    assert (first, second) == ("photo.png", "photo (2).png")
    jobs.signer.release_all()
    jobs.nam.settle()
    assert jobs.kinds(first)[-1] == "finished"
    assert jobs.kinds(second)[-1] == "finished"
    # A name is free again once its job is over.
    (tmp_path / "c").mkdir()
    assert jobs.store.upload_file(_file(tmp_path / "c", body=b"third")) == "photo.png"


def test_cancel_while_sending_aborts_and_reports_nothing_else(tmp_path):
    jobs = Jobs(tmp_path, replies=[error_reply(0)])
    name = jobs.store.upload_file(_file(tmp_path))
    put = jobs.nam.issued[-1]
    assert jobs.store.cancel_upload(name) is True
    assert put.aborted
    put.finish()                       # Qt answers an aborted request too
    assert jobs.kinds(name) == ["started", "cancelled"]
    assert jobs.store.upload_job(name) is None


def test_cancel_while_the_signer_is_asked_sends_nothing(tmp_path):
    jobs = Jobs(tmp_path, defer=True)
    name = jobs.store.upload_file(_file(tmp_path))
    assert jobs.store.cancel_upload(name) is True
    jobs.signer.release_all()           # the signer answers late
    assert jobs.nam.calls == []
    assert jobs.kinds(name) == ["started", "cancelled"]


def test_once_a_server_holds_the_file_cancel_is_refused(tmp_path):
    router_mirror = FakeBlossomServer(MIRROR)

    def responder(verb, request, body):
        host = request.url().host()
        return (router_mirror if host == "mirror.example" else jobs.server)(verb, request, body)

    jobs = Jobs(tmp_path, servers=(SERVER, MIRROR))
    jobs.nam._responder = responder
    jobs.signer.defer = True
    name = jobs.store.upload_file(_file(tmp_path))
    jobs.signer.release()               # the upload's token
    jobs.nam.settle_one()               # the primary has it; the copy waits on the signer
    assert jobs.store.upload_job(name).status == "mirroring"
    assert jobs.store.cancel_upload(name) is False
    jobs.signer.release_all()
    jobs.nam.settle()
    assert jobs.kinds(name)[-1] == "finished"


def test_a_failed_upload_is_retried_as_it_was(tmp_path):
    jobs = Jobs(tmp_path, replies=[error_reply(0)])
    name = jobs.store.upload_file(_file(tmp_path))
    jobs.nam.settle()
    assert jobs.kinds(name)[-1] == "failed"
    assert jobs.store.can_retry(name)
    jobs.store.retry_upload(name)
    jobs.nam.settle()
    assert jobs.kinds(name)[-1] == "finished"
    assert jobs.server.uploads == [BODY]
    assert not jobs.store.can_retry(name)


def test_a_pasted_image_is_retried_from_its_bytes(tmp_path):
    jobs = Jobs(tmp_path, replies=[error_reply(0)])
    name = jobs.store.add_bytes(BODY, label="Pasted Image.png", mime_type="image/png")
    jobs.nam.settle()
    jobs.store.retry_upload(name)
    jobs.nam.settle()
    assert jobs.kinds(name)[-1] == "finished"
    assert jobs.store.name_of(SHA) == "Pasted Image.png"


def test_a_dismissed_failure_cannot_be_retried(tmp_path):
    jobs = Jobs(tmp_path, replies=[error_reply(0)])
    name = jobs.store.upload_file(_file(tmp_path))
    jobs.nam.settle()
    jobs.store.dismiss_upload(name)
    assert not jobs.store.can_retry(name)
    assert jobs.store.upload_file(_file(tmp_path)) == name    # the name is free


def test_the_name_a_file_was_added_with_is_remembered(tmp_path):
    jobs = Jobs(tmp_path)
    jobs.store.upload_file(_file(tmp_path, "Holiday 2026.png"))
    jobs.nam.settle()
    assert jobs.store.name_of(SHA) == "Holiday 2026.png"
    assert MediaNames(tmp_path / "media_names.json").get(SHA) == "Holiday 2026.png"


def test_an_editor_upload_keeps_its_own_job_name_and_no_file_name(tmp_path):
    """The asset manager names its jobs by asset key and follows them by
    that name, so the store must not rename them; and an asset key is
    not a file name to remember."""
    jobs = Jobs(tmp_path)
    jobs.store.upload_bytes(BODY, name=f"asset:{SHA}", mime_type="image/png")
    jobs.nam.settle()
    assert jobs.kinds(f"asset:{SHA}")[-1] == "finished"
    assert jobs.store.name_of(SHA) == ""


def test_cancel_as_soon_as_the_upload_starts_sends_nothing(tmp_path):
    """The window's × can be pressed the moment the tile starts moving,
    which is while the store is still telling everyone it started."""
    jobs = Jobs(tmp_path)
    jobs.store.upload_started.connect(jobs.store.cancel_upload)
    name = jobs.store.upload_file(_file(tmp_path))
    jobs.nam.settle()
    assert jobs.nam.calls == []
    assert jobs.kinds(name) == ["started", "cancelled"]


def test_the_files_being_added_are_known_to_the_store(tmp_path):
    """A library window that opens later shows them: the store owns the
    jobs, not the window that started them."""
    jobs = Jobs(tmp_path, replies=[error_reply(0)])
    name = jobs.store.upload_file(_file(tmp_path, "Holiday.png"))
    jobs.nam.settle()
    [added] = jobs.store.added_uploads()
    assert (added.name, added.mime, added.state, added.path) == (
        name, "image/png", "failed", str(tmp_path / "Holiday.png"))
    assert "check your internet connection" in added.error


def test_adding_a_file_that_failed_again_retries_it_under_its_name(tmp_path):
    jobs = Jobs(tmp_path, replies=[error_reply(0)])
    path = _file(tmp_path, "Holiday.png")
    name = jobs.store.upload_file(path)
    jobs.nam.settle()
    assert jobs.store.upload_file(path) == name == "Holiday.png"
    jobs.nam.settle()
    assert jobs.kinds(name)[-1] == "finished"
    assert jobs.store.added_uploads() == []


def test_a_file_is_remembered_by_its_name_once_its_first_server_has_it(tmp_path):
    """Copying it to the other servers comes after: quitting then stops
    the copies, and the file stays in the library under its own name."""
    jobs = Jobs(tmp_path, servers=(SERVER, MIRROR))
    jobs.nam._responder = lambda verb, request, body: (
        FakeBlossomServer(MIRROR) if request.url().host() == "mirror.example"
        else jobs.server)(verb, request, body)
    jobs.signer.defer = True
    name = jobs.store.upload_file(_file(tmp_path, "Holiday 2026.png"))
    jobs.signer.release()
    jobs.nam.settle_one()
    assert jobs.store.upload_job(name).status == "mirroring"
    assert MediaNames(tmp_path / "media_names.json").get(SHA) == "Holiday 2026.png"


# -- uploads that must not outlive their account, and files sent again -----------

from nostr.imports import workers  # noqa: E402

THIRD = "https://third.example"
BIG = b"\x07" * workers.BACKGROUND_FROM_BYTES     # hashed on a worker thread


def _hashed() -> bool:
    """Every hash on a worker thread has been delivered."""
    deadline = time.monotonic() + 20
    while workers._ACTIVE_HOLDERS and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    QApplication.processEvents()
    return not workers._ACTIVE_HOLDERS


def _servers(jobs, *servers):
    by_host = {s.host: s for s in servers}
    jobs.nam._responder = lambda verb, request, body: by_host[request.url().host()](
        verb, request, body)


def test_cancel_while_a_large_file_is_hashed_sends_nothing(tmp_path):
    # RG-M2b 5: the job exists before its hash, so Cancel reaches it.
    jobs = Jobs(tmp_path)
    jobs.store.upload_bytes(BIG, name="big.smf1")
    assert jobs.store.cancel_upload("big.smf1") is True
    assert _hashed()
    jobs.nam.settle()
    assert jobs.signer.requests == [] and jobs.server.uploads == []
    assert jobs.kinds("big.smf1") == ["cancelled"]


@pytest.mark.parametrize("when", ["hashing", "signing", "copying"])
def test_leaving_the_account_stops_its_uploads_and_its_files_stay_out(tmp_path, when):
    # RG-M2b 5: nothing is signed as, sent for or put in the library of
    # the account after.
    mirror = FakeBlossomServer(MIRROR)
    jobs = Jobs(tmp_path, servers=(SERVER, MIRROR), defer=True)
    _servers(jobs, jobs.server, mirror)
    jobs.store.upload_bytes(BIG if when == "hashing" else BODY, name="a.smf1")
    if when != "hashing":
        jobs.signer.release()                       # the upload's token
        if when == "copying":
            jobs.nam.settle_one()                   # the first server has it
            assert jobs.store.upload_job("a.smf1").status == "mirroring"
    jobs.store.clear()                              # sign-out or another account
    assert _hashed()
    jobs.signer.release_all()
    jobs.nam.settle()
    assert jobs.store.files == {} and jobs.store.upload_job("a.smf1") is None
    assert mirror.uploads == [] and mirror.mirrors == []
    assert "finished" not in jobs.kinds("a.smf1") and jobs.kinds("a.smf1")[-1] == "cancelled"
    if when == "hashing":
        assert jobs.signer.requests == []


def test_a_file_a_server_says_is_missing_is_sent_again_though_the_library_has_it(tmp_path):
    # RG-M2b 4: force skips the library's word; without it nothing goes.
    jobs = Jobs(tmp_path)
    jobs.store.upload_bytes(BODY, name="a.smf1")
    jobs.nam.settle()
    assert len(jobs.server.uploads) == 1
    jobs.store.upload_bytes(BODY, name="b.smf1")
    jobs.nam.settle()
    assert len(jobs.server.uploads) == 1            # the library knows: no PUT
    jobs.store.upload_bytes(BODY, name="c.smf1", force=True)
    jobs.nam.settle()
    assert len(jobs.server.uploads) == 2 and jobs.kinds("c.smf1")[-1] == "finished"


def test_a_server_that_refuses_the_file_hands_it_to_every_other_and_names_them(tmp_path):
    # Contract 7 (ee3e5d7): 413 or 415 tries the writer's other servers;
    # when none takes it, the servers and the reasons are named: in its
    # Details, never its one sentence (lessons 36).
    too_large = FakeBlossomServer(SERVER, fail_with=413)
    wrong_type = FakeBlossomServer(MIRROR, fail_with=415)
    third = FakeBlossomServer(THIRD, fail_with=413)
    jobs = Jobs(tmp_path, servers=(SERVER, MIRROR, THIRD))
    _servers(jobs, too_large, wrong_type, third)
    said = []
    jobs.store.upload_failed.connect(lambda _n, reason: said.append(reason))
    jobs.store.upload_bytes(BODY, name="a.smf1")
    jobs.nam.settle()
    assert len(jobs.signer.requests) == 3           # every server was asked
    assert said == ["No media server took the file: add another one in Media Servers."]
    assert jobs.store.refusal_detail("a.smf1") == ("good.example: too large\n"
                                                    "mirror.example: not a type it takes\n"
                                                    "third.example: too large")
    taken = FakeBlossomServer(THIRD)
    _servers(jobs, too_large, wrong_type, taken)
    jobs.store.upload_bytes(BODY, name="b.smf1")
    jobs.nam.settle()
    assert len(taken.uploads) == 1 and jobs.kinds("b.smf1")[-1] == "finished"


def test_one_server_that_refuses_says_its_remedy_and_names_it_only_in_the_details(tmp_path):
    jobs = Jobs(tmp_path)
    _servers(jobs, FakeBlossomServer(SERVER, fail_with=413))
    said = []
    jobs.store.upload_failed.connect(lambda _n, reason: said.append(reason))
    jobs.store.upload_bytes(BODY, name="a.smf1")
    jobs.nam.settle()
    assert said == ["This file is too large for your media server: choose a smaller one, or "
                    "add a server in Media Servers."]
    assert jobs.store.refusal_detail("a.smf1") == "good.example: too large"
    # A later try that fails another way names no servers.
    _servers(jobs, FakeBlossomServer(SERVER, fail_with=500))
    jobs.store.upload_bytes(BODY, name="a.smf1")
    jobs.nam.settle()
    assert len(said) == 2 and jobs.store.refusal_detail("a.smf1") == ""


def test_only_the_newest_refusals_are_kept_for_their_details(tmp_path):
    from nostr.blossom import store as store_module
    jobs = Jobs(tmp_path)
    _servers(jobs, FakeBlossomServer(SERVER, fail_with=415))
    for i in range(store_module._REFUSALS_KEPT + 1):
        jobs.store.upload_bytes(BODY + bytes([i]), name=f"{i}.smf1")
        jobs.nam.settle()
    assert jobs.store.refusal_detail("0.smf1") == ""
    assert jobs.store.refusal_detail(f"{store_module._REFUSALS_KEPT}.smf1") == \
        "good.example: not a type it takes"


def test_the_servers_that_refused_are_logged_and_forgotten_with_the_account(tmp_path, caplog):
    import logging
    jobs = Jobs(tmp_path)
    _servers(jobs, FakeBlossomServer(SERVER, fail_with=413))
    with caplog.at_level(logging.INFO, logger="nostr.blossom.store"):
        jobs.store.upload_bytes(BODY, name="a.smf1")
        jobs.nam.settle()
    assert any("a.smf1" in r.getMessage() and "good.example: too large" in r.getMessage()
               for r in caplog.records)
    jobs.store.clear()                               # the account is left
    assert jobs.store.refusal_detail("a.smf1") == ""


def test_a_server_that_kept_no_copy_is_logged_and_told_never_said(tmp_path, caplog):
    # Lessons 36: the file is on the first server, so the upload worked.
    import logging
    jobs = Jobs(tmp_path, servers=(SERVER, MIRROR))
    _servers(jobs, jobs.server, FakeBlossomServer(MIRROR, fail_with=500))
    missed, said = [], []
    jobs.store.mirror_failed.connect(lambda n, host, code: missed.append((n, host)))
    jobs.store.upload_failed.connect(lambda n, reason: said.append(reason))
    with caplog.at_level(logging.INFO, logger="nostr.blossom.store"):
        jobs.store.upload_bytes(BODY, name="a.smf1")
        jobs.nam.settle()
    assert jobs.kinds("a.smf1")[-1] == "finished" and said == []
    assert missed == [("a.smf1", "mirror.example")]
    assert any("a.smf1 has no copy on mirror.example" in r.getMessage() for r in caplog.records)


def test_an_added_file_every_server_refused_keeps_their_names_for_its_tooltip(tmp_path):
    # The Media Library's line names them in its tooltip, also in a
    # window opened later (added_uploads), and a retry forgets them.
    jobs = Jobs(tmp_path, servers=(SERVER, MIRROR))
    _servers(jobs, FakeBlossomServer(SERVER, fail_with=413),
             FakeBlossomServer(MIRROR, fail_with=415))
    name = jobs.store.upload_file(_file(tmp_path, "Holiday.png"))
    jobs.nam.settle()
    named = "good.example: too large\nmirror.example: not a type it takes"
    assert jobs.store.refusal_detail(name) == named
    [added] = jobs.store.added_uploads()
    assert added.detail == named and "good.example" not in added.error
    _servers(jobs, FakeBlossomServer(SERVER, fail_with=500), FakeBlossomServer(MIRROR))
    jobs.store.retry_upload(name)
    jobs.nam.settle()
    assert jobs.store.refusal_detail(name) == ""
