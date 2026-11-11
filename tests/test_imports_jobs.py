# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Imports as durable jobs: pause, stop, resume, restart, retry.

The pipeline and the catalogue are faked; the job database is real (in
memory), so what survives a restart is what the database says.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from nostr.imports import snapshots
from nostr.imports.inbox_store import InboxStore
from nostr.imports.jobs import NO_RELAY, ImportRunner
from tests.imports_fakes import FakeCatalogue, make_item

PROFILE = object()


class FakeItemJob(QObject):
    item_started = Signal(int, str)
    item_resolving_from_nostr = Signal(int, str)
    item_extracting = Signal(int, str)
    item_mirroring = Signal(int, int, int, int)
    item_signed = Signal(int, dict)
    item_existing = Signal(int, str)
    item_failed = Signal(int, str)
    item_published = Signal(int, int, int)
    completed = Signal(int, int)

    def __init__(self, harness, items, is_imported, kwargs, parent=None):
        super().__init__(parent)
        self.harness = harness
        self.item = items[0]
        self.is_imported = is_imported
        self.kwargs = kwargs

    def start(self):
        d_tag = snapshots.identifier_of(self.item)
        self.harness.started.append(d_tag)
        script = self.harness.script.get(self.item.title, "ok")
        if script == "hold":
            self.harness.held.append(self)
            return
        self.play(script)

    def play(self, script):
        d_tag = snapshots.identifier_of(self.item)
        if self.is_imported(d_tag):
            self.item_existing.emit(0, d_tag)
            self.completed.emit(0, 0)
            return
        self.item_started.emit(0, self.item.title)
        if script == "fail":
            self.item_failed.emit(0, "could not read it")
            self.completed.emit(0, 1)
            return
        self.item_signed.emit(0, {"id": "wrap-" + d_tag, "kind": 31234})
        accepted = 0 if script == "norelay" else 1
        self.item_published.emit(0, accepted, 2)
        self.completed.emit(1, 1)


class FakeResend(QObject):
    completed = Signal(list)
    failed = Signal(str)

    def __init__(self, harness, identifier):
        super().__init__()
        self.harness = harness
        self.identifier = identifier

    def send_signed(self, event):
        self.harness.resent.append(event["id"])
        if self.harness.resend_fails:
            self.words = "Your signer app declined the request: allow it there and try again."
            self.failed.emit("user rejected")
            return
        self.completed.emit([("wss://r", True, "")])


class Harness:
    def __init__(self, store=None, catalogue=None, script=None):
        self.store = store or InboxStore(":memory:", clock=lambda: 1_800_000_000)
        self.catalogue = catalogue or FakeCatalogue()
        self.script = dict(script or {})
        self.started, self.held, self.resent, self.kwargs = [], [], [], []
        self.resend_fails = False
        self.runner = self.make_runner()

    def make_runner(self):
        def item_job(**kwargs):
            self.kwargs.append(kwargs)
            return FakeItemJob(self, kwargs["items"], kwargs["is_imported"], kwargs,
                               parent=kwargs.get("parent"))

        return ImportRunner(store=self.store, catalogue=self.catalogue, profile=PROFILE,
                            item_job_factory=item_job,
                            resend_factory=lambda identifier, parent=None: FakeResend(
                                self, identifier),
                            pacer=lambda ms, fn: fn())

    def job(self, *titles, **kw):
        posts = [(make_item(t, guid=f"g-{t}", content_html="<p>body</p>"), "") for t in titles]
        return self.runner.create(label="Field notes", source_type="feed", posts=posts,
                                  source_url="https://blog.example/feed", **kw)

    def status(self, job):
        saved = self.store.job(job.id)
        return saved.status, [row.status for row in saved.rows]


def d(title):
    return snapshots.identifier_of(make_item(title, guid=f"g-{title}"))


def test_a_job_is_saved_paused_and_runs_to_completion():
    h = Harness()
    job = h.job("a", "b")
    assert h.status(job) == ("paused", ["pending", "pending"])
    created = []
    h.runner.draft_created.connect(created.append)
    assert h.runner.run(job.id)
    assert h.status(job) == ("completed", ["done", "done"])
    assert created == [d("a"), d("b")]
    assert h.store.ledger_states([d("a")]) == {d("a"): "drafted"}
    assert all(row.signed_event is None for row in h.store.job(job.id).rows)
    assert h.kwargs[0]["feed_url"] == "https://blog.example/feed"


def test_posts_already_there_are_never_signed():
    h = Harness(catalogue=FakeCatalogue(existing={d("a"): "published"}))
    h.catalogue.local[d("c")] = "drafted"
    job = h.job("a", "b", "c")
    h.runner.run(job.id)
    assert h.status(job) == ("completed", ["existing", "done", "existing"])
    assert h.started == [d("b")]
    assert h.store.ledger_states([d("a")]) == {d("a"): "published"}


def test_no_answer_about_existing_drafts_pauses_with_the_reason():
    h = Harness(catalogue=FakeCatalogue(unavailable="Couldn't check."))
    job = h.job("a")
    h.runner.run(job.id)
    saved = h.store.job(job.id)
    assert (saved.status, saved.error) == ("paused", "Couldn't check.")
    assert h.started == []
    assert not h.runner.busy()


def test_pause_takes_effect_between_posts_and_resume_does_the_rest():
    h = Harness(script={"a": "hold"})
    job = h.job("a", "b", "c")
    h.runner.run(job.id)
    h.runner.pause()
    assert h.store.job(job.id).status == "pausing"
    h.held.pop().play("ok")
    assert h.status(job) == ("paused", ["done", "pending", "pending"])
    h.script = {}
    h.runner.run(job.id)
    assert h.status(job) == ("completed", ["done", "done", "done"])
    assert h.started == [d("a"), d("b"), d("c")]


def test_stop_is_final_and_keeps_what_was_made():
    h = Harness(script={"a": "hold"})
    job = h.job("a", "b")
    h.runner.run(job.id)
    h.runner.stop()
    h.held.pop().play("ok")
    assert h.status(job) == ("stopped", ["done", "pending"])
    assert h.runner.run(job.id) is False


def test_a_restart_pauses_and_a_kept_draft_is_sent_again_without_signing():
    h = Harness(script={"b": "hold"})
    job = h.job("a", "b")
    h.runner.run(job.id)
    # "b" was signed and kept, then the app went away before it was sent.
    row = h.store.job(job.id).rows[1]
    row.signed_event = {"id": "wrap-kept", "kind": 31234}
    h.store.save_row(job.id, row)
    h.store.recover_jobs()
    assert h.store.job(job.id).status == "paused"
    again = Harness(store=h.store)
    again.runner.run(job.id)
    assert again.resent == ["wrap-kept"]
    assert again.started == []
    assert again.status(job) == ("completed", ["done", "done"])


def test_failed_posts_make_a_partial_job_and_only_they_are_tried_again():
    h = Harness(script={"b": "fail"})
    job = h.job("a", "b")
    h.runner.run(job.id)
    saved = h.store.job(job.id)
    assert (saved.status, saved.rows[1].error) == ("partial", "could not read it")
    h.script = {}
    h.started.clear()
    h.runner.run(job.id)
    assert h.started == [d("b")]
    assert h.status(job) == ("completed", ["done", "done"])


def test_a_draft_no_relay_took_is_kept_and_sent_again():
    h = Harness(script={"a": "norelay"})
    job = h.job("a")
    h.runner.run(job.id)
    saved = h.store.job(job.id)
    assert saved.status == "partial"
    assert saved.rows[0].error == NO_RELAY
    assert saved.rows[0].signed_event == {"id": "wrap-" + d("a"), "kind": 31234}
    h.runner.run(job.id)
    assert h.resent == ["wrap-" + d("a")]
    assert h.status(job) == ("completed", ["done"])


def test_a_draft_sent_again_that_fails_keeps_the_job_s_one_sentence():
    # Lessons 36: the post's line is the draft job's sentence, never the
    # signer's own words.
    h = Harness(script={"a": "norelay"})
    job = h.job("a")
    h.runner.run(job.id)
    h.resend_fails = True
    h.runner.run(job.id)
    saved = h.store.job(job.id)
    assert saved.rows[0].error == (
        "Your signer app declined the request: allow it there and try again.")


def test_one_job_at_a_time():
    h = Harness(script={"a": "hold"})
    first = h.job("a")
    second = h.job("b")
    assert h.runner.run(first.id)
    assert h.runner.run(second.id) is False


def test_an_account_change_pauses_at_once():
    h = Harness(script={"a": "hold"})
    job = h.job("a", "b")
    h.runner.run(job.id)
    h.runner.halt("The account changed.")
    saved = h.store.job(job.id)
    assert (saved.status, saved.error) == ("paused", "The account changed.")
    # The post in flight is tried again on resume.
    assert [row.status for row in saved.rows] == ["pending", "pending"]
    h.held.pop().play("ok")            # a late answer changes nothing
    assert h.status(job) == ("paused", ["pending", "pending"])


def test_a_sources_defaults_apply_where_the_run_chose_nothing():
    h = Harness()
    item = make_item("a", guid="g-a")
    job = h.runner.create(label="x", source_type="inbox", posts=[(item, "src")],
                          options={"skip_image_urls": ["https://x/1.png"],
                                   "by_source": {"src": {"rehost_images": False,
                                                         "fetch_full_text": True}}})
    h.runner.run(job.id)
    kwargs = h.kwargs[0]
    assert (kwargs["rehost_images"], kwargs["fetch_full_text"]) == (False, True)
    assert kwargs["skip_image_urls"] == {"https://x/1.png"}


def test_a_choice_for_the_run_wins_over_a_sources_default():
    h = Harness()
    item = make_item("a", guid="g-a")
    job = h.runner.create(label="x", source_type="inbox", posts=[(item, "src")],
                          options={"rehost_images": True,
                                   "by_source": {"src": {"rehost_images": False,
                                                         "fetch_full_text": False}}})
    h.runner.run(job.id)
    kwargs = h.kwargs[0]
    assert (kwargs["rehost_images"], kwargs["fetch_full_text"]) == (True, False)


def test_finished_row_jobs_are_let_go():
    """Engine review M5: every post's job stayed a child of the runner
    for the whole session."""
    from PySide6.QtCore import QCoreApplication, QEvent
    h = Harness()
    job = h.job("a", "b", "c")
    h.runner.run(job.id)
    assert h.status(job)[0] == "completed"
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not h.runner.findChildren(FakeItemJob)


def test_a_kept_draft_near_its_end_is_made_anew():
    """Engine review L5: a kept signed draft past its expiration was sent
    again and refused by every relay, for good."""
    h = Harness()
    job = h.job("a")
    saved = h.store.job(job.id)
    row = saved.rows[0]
    row.signed_event = {"id": "old-wrap", "kind": 31234,
                        "tags": [["d", row.d_tag], ["expiration", "1000"]]}
    h.store.save_row(job.id, row)
    h.runner.run(job.id)
    assert h.resent == []                      # not the expired one
    assert h.started == [row.d_tag]            # made again
    assert h.status(job) == ("completed", ["done"])


def test_a_job_with_failed_posts_is_not_pruned():
    """Engine review L12: a job whose failed posts can be tried again was
    deleted after a week like a finished one."""
    clock = {"now": 1_800_000_000}
    store = InboxStore(":memory:", clock=lambda: clock["now"])
    h = Harness(store=store, script={"b": "fail"})
    job = h.job("a", "b")
    h.runner.run(job.id)
    assert h.status(job)[0] == "partial"
    done = h.job("c")
    h.runner.run(done.id)
    clock["now"] += 8 * 24 * 3600
    store.prune_jobs()
    assert store.job(job.id) is not None
    assert store.job(done.id) is None
