# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How an import is going, in one place.

The toolbar's activity button shows an import that has posts to do;
its card pauses, resumes, stops (after asking) and tries failed posts
again. The posts an import holds say Importing, those it could not make
say Failed. When an import went through all its posts, the window says
so once, with Show Drafts.
"""

from __future__ import annotations

import pytest

from nostr.imports.inbox_store import Job, JobRow
from nostr.ui.imports_activity import (
    activity_line,
    counts_text,
    finished_text,
    progress_text,
    stage_text,
    status_text,
)
from nostr.ui.imports_window import ImportsWindow
from tests.accessibility import unnamed_controls
from tests.outbox_fakes import settle
from tests.test_imports_create import Jobs, make_controller
from tests.widget_lifetime import delete_new_windows


@pytest.fixture(autouse=True)
def _windows_deleted():
    """Every window and panel a test makes is deleted after it: left to
    the cycle collector, one without a parent can crash it."""
    yield from delete_new_windows()


@pytest.fixture
def jobs():
    return Jobs()


@pytest.fixture
def setup(tmp_path, jobs):
    controller = make_controller(tmp_path, jobs)
    shown = []
    stops = []
    win = ImportsWindow(controller, show_drafts=lambda: shown.append(True),
                        confirm=lambda **asked: stops.append(asked) or win.allow_stop)
    win.allow_stop = True
    win.resize(1180, 760)
    win.show()
    settle()
    yield win, controller, jobs, shown, stops
    win.close()
    controller.account_changed(None)
    settle()


def hold_all(win, jobs):
    model = win.posts.model_
    jobs.script = {model.post(r).title: "hold" for r in range(model.rowCount())}


def start_two(win):
    model = win.posts.model_
    model.set_checked(0, True)
    model.set_checked(1, True)
    titles = [model.post(0).title, model.post(1).title]
    win.create_drafts()
    settle()
    return titles


class TestTheButton:
    def test_hidden_until_an_import_runs(self, setup):
        win, _controller, jobs, _shown, _stops = setup
        assert win.activity_button.isHidden()
        hold_all(win, jobs)
        start_two(win)
        assert not win.activity_button.isHidden()
        assert win.activity_button.text() == "0 of 2"
        assert win.activity_button.accessibleName() == (
            "Import progress: Creating drafts, 0 of 2")

    def test_it_counts_up_and_goes_when_done(self, setup):
        win, _controller, jobs, shown, _stops = setup
        hold_all(win, jobs)
        start_two(win)
        jobs.held.pop(0).play("ok")
        settle()
        assert win.activity_button.text() == "1 of 2"
        jobs.held.pop(0).play("ok")
        settle()
        assert win.activity_button.isHidden()
        assert win.banner.label.text() == "Import finished. 2 drafts created."
        win.banner.action.click()
        assert shown == [True]

    def test_the_posts_it_holds_say_importing(self, setup):
        win, _controller, jobs, _shown, _stops = setup
        hold_all(win, jobs)
        titles = start_two(win)
        model = win.posts.model_
        words = {model.post(r).title: model.word_for(model.post(r))
                 for r in range(model.rowCount())}
        assert {words[t] for t in titles} == {"Importing"}


class TestTheCard:
    def test_pause_then_resume(self, setup):
        win, controller, jobs, _shown, _stops = setup
        hold_all(win, jobs)
        start_two(win)
        win._show_job_card()
        card = win.job_card
        assert card.go_button.text() == "Pause"
        assert not any(w.isHidden() for w in (card.status, card.bar, card.progress, card.counts))
        card.go_button.click()
        assert card.status.text() == "Pausing after this post"
        jobs.held.pop(0).play("ok")
        settle()
        assert card.status.text() == "Import paused"
        assert win.activity_button.text() == "1 of 2"
        assert card.go_button.text() == "Resume"
        card.go_button.click()
        settle()
        jobs.held.pop(0).play("ok")
        settle()
        assert controller.activity() is None

    def test_stop_asks_first(self, setup):
        win, controller, jobs, _shown, stops = setup
        hold_all(win, jobs)
        start_two(win)
        win._show_job_card()
        win.allow_stop = False
        win.job_card.stop_button.click()
        assert stops[0]["title"] == "Stop this import?"
        assert stops[0]["action"] == "Stop Import"
        assert controller.activity().status == "running"
        win._show_job_card()
        win.allow_stop = True
        win.job_card.stop_button.click()
        assert controller.activity().status == "stopping"
        jobs.held.pop(0).play("ok")
        settle()
        # Stopped for good: nothing left to show, the draft made stays.
        assert controller.activity() is None
        assert controller.counts().imported == 1

    def test_failed_posts_can_be_tried_again(self, setup):
        win, controller, jobs, _shown, _stops = setup
        model = win.posts.model_
        failing = model.post(0).title
        jobs.script = {failing: "fail"}
        model.set_checked(0, True)
        win.create_drafts()
        settle()
        job = controller.activity()
        assert job.status == "partial"
        # Lessons 36: what did not happen, never how many.
        assert win.activity_button.text() == "Not all imported"
        assert win.activity_button.accessibleName() == "Your post wasn't imported"
        assert win.banner.label.text() == (
            "Your post wasn't imported: open the import’s progress and choose Try Again.")
        row = next(r for r in range(model.rowCount()) if model.post(r).title == failing)
        assert model.word_for(model.post(row)) == "Failed"
        win._show_job_card()
        card = win.job_card
        assert card.go_button.text() == "Try Again"
        from nostr.failures import markup
        assert card.problem.text() == markup("Your post wasn't imported", "could not read it")
        assert card.problem.accessibleName() == "Your post wasn't imported. could not read it."
        assert card.problem.toolTip() == f"“{failing}”: could not read it"
        # Said once, in the problem line: no status repeating it, no
        # bar, no "0 of 1", no "0 created".
        assert [w.isHidden() for w in (card.status, card.bar, card.progress, card.counts)] == \
            [True] * 4
        assert not card.problem.isHidden()
        jobs.script = {}
        card.go_button.click()
        settle()
        assert controller.activity() is None
        assert controller.counts().imported == 1

    def test_a_failed_post_imported_later_is_imported(self, setup):
        """CP2 review N8: a post that failed in one import and became a
        draft later (a second Create, or in the other app) still read
        "Failed", and the import kept saying "1 failed"."""
        win, controller, jobs, _shown, _stops = setup
        model = win.posts.model_
        failing = model.post(0)
        jobs.script = {failing.title: "fail"}
        model.set_checked(0, True)
        win.create_drafts()
        settle()
        assert controller.activity().status == "partial"
        controller.inbox.reconcile(drafted=[failing.d_tag])     # made elsewhere
        controller._announce()
        settle()
        assert controller.activity() is None
        assert controller.posts_in_progress() == (set(), set())
        win.sidebar.select("list", "imported")
        settle()
        row = next(r for r in range(model.rowCount()) if model.post(r).d_tag == failing.d_tag)
        assert model.word_for(model.post(row)) == ""            # Imported, said by the list
        assert win.activity_button.isHidden()

    def test_the_card_has_names(self, setup):
        win, _controller, jobs, _shown, _stops = setup
        hold_all(win, jobs)
        start_two(win)
        win._show_job_card()
        assert unnamed_controls(win.job_card) == []
        assert unnamed_controls(win) == []


def job(status="running", rows=(), error=""):
    return Job(id="j", label="Inbox", source_type="inbox", source_url="", status=status,
               options={}, created_at=0, updated_at=0, error=error, rows=list(rows))


def row(status="pending", stage="", title="A post", error=""):
    return JobRow(index=0, d_tag="d", title=title, status=status, stage=stage, error=error)


def test_the_words():
    running = job(rows=[row("done"), row("existing"), row("pending", stage="images")])
    assert progress_text(running) == "2 of 3"
    assert status_text(running) == "Creating drafts"
    assert counts_text(running) == "1 created, 1 was already there"
    assert stage_text(running) == "Copying the images of “A post”…"
    assert activity_line(running) == "Creating drafts, 2 of 3"
    paused = job("paused", rows=[row("done"), row("pending")], error="The account changed.")
    assert activity_line(paused) == "Import paused, 1 of 2"
    assert stage_text(paused) == ""
    partial = job("partial", rows=[row("done"), row("failed"), row("failed")])
    assert status_text(partial) == "Some posts weren't imported"
    assert counts_text(partial) == "1 created"              # never how many failed
    assert finished_text(partial) == ("Some posts weren't imported: open the import’s progress "
                                      "and choose Try Again.")
    assert status_text(job("partial", rows=[row("failed")])) == "Your post wasn't imported"
    assert status_text(job("partial", rows=[row("failed")] * 2)) == "Your posts weren't imported"
    assert activity_line(None) == ""
    done = job("completed", rows=[row("done"), row("existing"), row("existing")])
    assert finished_text(done) == ("Import finished. 1 draft created. 2 were already there.")


def test_the_drafts_panel_row_follows_the_import(setup):
    """The Drafts panel's Imports row (D-2) says what the Imports window's
    toolbar says, and its Pause and Resume act on the same import."""
    from types import SimpleNamespace
    from main_window import MainWindow
    from nostr.ui.drafts_panel import DraftsPanel
    win, controller, jobs, _shown, _stops = setup
    panel = DraftsPanel(is_dark=True)
    panel.pause_import.connect(controller.pause_import)
    host = SimpleNamespace(_imports=controller, _drafts_panel=panel)
    host._resume_import = lambda: MainWindow._resume_import(host)
    panel.resume_import.connect(host._resume_import)
    MainWindow._update_imports_row(host)
    row = panel._imports_row
    assert not row.isHidden()
    assert row.button.count_text() == f"{controller.counts().inbox} new"
    assert row._activity_line.isHidden()
    hold_all(win, jobs)
    start_two(win)
    MainWindow._update_imports_row(host)
    assert row.activity.text() == "Creating drafts, 0 of 2"
    assert row.action.text() == "Pause"
    row.action.click()
    jobs.held.pop(0).play("ok")
    settle()
    MainWindow._update_imports_row(host)
    assert row.activity.text() == "Import paused, 1 of 2"
    assert row.action.text() == "Resume"
    row.action.click()
    settle()
    jobs.held.pop(0).play("ok")
    settle()
    MainWindow._update_imports_row(host)
    assert row._activity_line.isHidden()
    controller.read_only = True
    MainWindow._update_imports_row(host)
    assert row.action.isHidden()


def test_an_ellipsis_stays_with_its_word():
    """German writes a space before the ellipsis ("wird vorbereitet ..."),
    and a wrapped line put the ellipsis alone on the next line."""
    from nostr.ui.imports_activity import with_its_word
    assert with_its_word("„A post“ wird vorbereitet …") == (
        "„A post“ wird vorbereitet …")
    assert with_its_word("Preparing “A post”…") == "Preparing “A post”…"
