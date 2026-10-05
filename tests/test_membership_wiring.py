# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The membership seams the application actually connects, and how they behave.

``nostr/einundzwanzig.py`` resolves membership and ``MediaStore`` and the
publish jobs accept an entitlement, and each of those is covered on its
own. ``MembershipController`` is what joins them in the running app: it
owns the roster and its refresh, answers what the active account is
entitled to, runs the membership window, and says when the benefits
changed. Its behaviour is tested here directly, with fakes for the
roster's transport, the API and the relay jobs.

``MainWindow`` cannot be constructed in a test (it reads the real
settings and builds a relay pool), so what it does with the controller is
checked two ways: a few structural checks that it builds one, starts it
and tells it about account changes, and its two membership handlers run
on a stand-in object, the way tests/test_media_document.py lifts methods.

One distinction is deliberate and worth pinning. A dialog receives the
provider itself, because it can sit open for minutes and membership may
resolve while it does. A job receives the already-resolved list, because
by then the answer must be fixed. Passing the wrong one either way is
silent: a callable reads as truthy, so a job handed a provider would
publish to a relay list containing a function.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal  # noqa: E402
from PySide6.QtNetwork import QNetworkReply  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from main_window import MainWindow  # noqa: E402
from nostr import membership_controller as mc  # noqa: E402
from nostr import relay_list_addition as rla  # noqa: E402
from nostr.einundzwanzig import (  # noqa: E402
    MEMBER_BLOSSOM, MEMBER_RELAY, PER_USER_BYTES, MembershipDirectory,
)
from nostr.membership_controller import MembershipController  # noqa: E402
from nostr.ui import membership_window as mw  # noqa: E402
from nostr.ui.membership_window import MEMBER  # noqa: E402
from tests.test_einundzwanzig import FakeNam, roster_bytes  # noqa: E402
from tests.test_media_wiring import (  # noqa: E402
    attribute_argument, calls, constructor, init_body, statement_index,
)

MEMBER_KEY = "a" * 64
STRANGER = "b" * 64

# Re-exported so the fixture resolves in this module.
init_body = init_body


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def quiet_alerts(monkeypatch):
    monkeypatch.setattr(mw, "inform", lambda parent, **kw: None)
    monkeypatch.setattr(mw, "confirm_destructive", lambda parent, **kw: True)


def flush_deletes():
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


# --------------------------------------------------------------------- #
# Fakes                                                                  #
# --------------------------------------------------------------------- #

class Profile:
    def __init__(self, pubkey=MEMBER_KEY, *, local=False):
        self.user_pubkey = pubkey
        self.is_local = local


class Api(QObject):
    """The membership client's shape, as a QObject so ownership shows."""

    configured = True

    def __init__(self, *, answer_later=False):
        super().__init__()
        self.answer_later = answer_later
        self.pending_check = None
        self.canceled = 0
        self.calls = []

    def check_service(self, on_done):
        if self.answer_later:
            self.pending_check = lambda: on_done(True)
        else:
            on_done(True)

    def config(self, ok, fail):
        self.calls.append("config")

    def me(self, ok, fail):
        self.calls.append("me")

    def erase(self, ok, fail):
        self.calls.append("erase")
        self.erased = ok

    def apply(self, ok, fail, **kw):
        self.calls.append("apply")
        self.applied = ok

    def cancel(self):
        self.canceled += 1


class Job(QObject):
    finished = Signal(str)

    def __init__(self):
        super().__init__()
        self.started = 0

    def start(self):
        self.started += 1


class Harness:
    """A controller over a real directory with a fake roster transport."""

    def __init__(self, *, roster=(MEMBER_KEY,), profile=None, now=1000.0):
        self.profile = profile if profile is not None else Profile()
        self.clock = {"t": now}
        self.nam = FakeNam({2026: roster_bytes(*roster)})
        self.directory = MembershipDirectory(nam=self.nam, clock=lambda: self.clock["t"])
        self.directory._current_year = lambda: 2026
        self.apis = []
        self.jobs = []
        self.status = []
        self.links = []
        self.connects = 0
        self.controller = MembershipController(
            profile_provider=lambda: self.profile,
            directory=self.directory,
            api_factory=self.make_api,
            show_status=lambda text, ms: self.status.append(text),
            open_link=self.links.append,
            connect_signer=self.connect,
            add_relay_job=self.make_job,
            publish_list_job=self.make_job,
        )
        self.changes = []
        self.controller.benefits_changed.connect(lambda: self.changes.append(True))
        self.on_connect = None

    def make_api(self, profile):
        api = Api(answer_later=getattr(self, "answer_later", False))
        api.profile = profile
        self.apis.append(api)
        return api

    def make_job(self, profile):
        job = Job()
        job.profile = profile
        self.jobs.append(job)
        return job

    def connect(self):
        self.connects += 1
        if self.on_connect is not None:
            self.on_connect()

    def answer(self):
        """Settle the newest roster request."""
        self.nam.replies[-1].finished.emit()


def member_harness(**kw):
    h = Harness(**kw)
    h.controller.start()
    h.answer()
    return h


# --------------------------------------------------------------------- #
# What the active account is entitled to                                 #
# --------------------------------------------------------------------- #

def test_a_member_is_given_the_relay_the_server_its_space_and_its_name():
    c = member_harness().controller
    assert c.entitled_relays() == [MEMBER_RELAY]
    assert c.entitled_blossom_servers() == [MEMBER_BLOSSOM]
    assert c.quota(MEMBER_BLOSSOM) == PER_USER_BYTES
    assert c.quota(MEMBER_BLOSSOM + "/") == PER_USER_BYTES
    assert c.server_label(MEMBER_BLOSSOM) == "EINUNDZWANZIG"
    assert c.quota("https://other.example") is None
    assert c.server_label("https://other.example") is None


def test_a_non_member_and_nobody_are_given_nothing():
    h = member_harness(roster=(STRANGER,))
    assert h.controller.entitled_relays() == []
    assert h.controller.entitled_blossom_servers() == []
    assert h.controller.quota(MEMBER_BLOSSOM) is None
    h.profile = None
    assert h.controller.entitled_relays() == []


def test_nothing_is_granted_before_the_roster_answers():
    h = Harness()
    h.controller.start()
    assert h.controller.entitled_relays() == []


def test_a_stale_roster_does_not_drop_a_members_benefits():
    h = member_harness()
    h.clock["t"] += 60 * 60                        # long past the cache
    assert h.controller.entitled_blossom_servers() == [MEMBER_BLOSSOM]


def test_a_failed_refresh_does_not_drop_a_members_benefits():
    h = member_harness()
    h.nam.error = QNetworkReply.HostNotFoundError
    h.controller.refresh(force=True)
    h.answer()
    assert h.controller.entitled_relays() == [MEMBER_RELAY]
    assert h.changes == [True]                     # only the first answer changed anything


# --------------------------------------------------------------------- #
# Refreshing                                                             #
# --------------------------------------------------------------------- #

def test_start_resolves_the_account_already_signed_in_and_keeps_refreshing():
    h = Harness()
    h.controller.start()
    assert h.nam.requested == [2026]
    assert h.controller._timer.isActive()
    assert h.controller._timer.interval() == mc.REFRESH_INTERVAL_MS


def test_every_tick_fetches_the_roster_even_while_it_is_fresh():
    # The tick is shorter than the cache; without forcing, a third of the
    # ticks would answer from the cache and the roster would run stale.
    h = member_harness()
    h.controller._timer.timeout.emit()
    assert h.nam.requested == [2026, 2026]


def test_a_tick_that_changes_nothing_tells_nobody():
    h = member_harness()
    assert h.changes == [True]
    h.controller._timer.timeout.emit()
    h.answer()
    assert h.changes == [True]                     # nothing changed, nothing announced
    assert h.status.count(mc.RECOGNIZED_MESSAGE) == 1


def test_a_lapsed_membership_is_announced_as_a_change():
    h = member_harness()
    h.nam.by_year[2026] = roster_bytes(STRANGER)
    h.controller._timer.timeout.emit()
    h.answer()
    assert h.changes == [True, True]
    assert h.controller.entitled_relays() == []


def test_the_recognition_is_announced_once_in_plain_consistent_words():
    h = member_harness()
    assert h.status == [mc.RECOGNIZED_MESSAGE]
    assert mc.RECOGNIZED_MESSAGE.startswith("EINUNDZWANZIG membership recognized.")
    assert "\u2014" not in mc.RECOGNIZED_MESSAGE


def test_an_answer_for_another_account_is_ignored():
    h = member_harness(profile=Profile(STRANGER), roster=(MEMBER_KEY,))
    h.controller._on_resolved(MEMBER_KEY, True)
    assert h.status == [] and h.changes == []


# --------------------------------------------------------------------- #
# The membership window                                                  #
# --------------------------------------------------------------------- #

def test_the_window_is_told_what_is_last_known_about_the_account():
    # A member whose roster is being refreshed sees what they have, not a
    # join prompt.
    h = member_harness()
    h.clock["t"] += 60 * 60
    h.controller.open_window()
    window = h.controller.window
    assert window.page == MEMBER
    window.close()


def test_an_account_whose_key_is_here_is_not_sent_to_a_signer_app():
    h = member_harness(profile=Profile(local=True))
    h.controller.open_window()
    assert h.controller.window._signs_locally
    h.controller.window.close()


def test_reopening_after_a_late_answer_neither_crashes_nor_leaks():
    h = Harness(roster=(STRANGER,))
    h.answer_later = True
    h.controller.open_window()
    first_window, first_api = h.controller.window, h.apis[0]
    assert first_api.parent() is first_window      # owned, so it goes with it
    first_window.reject()
    h.controller.open_window()                     # rebuilt: the old one is deleted
    flush_deletes()
    assert not shiboken6.isValid(first_window)
    assert not shiboken6.isValid(first_api)
    first_api.pending_check()                      # the old client's late answer
    second = h.controller.window
    assert second is not first_window and shiboken6.isValid(second)
    second.close()


def test_closing_the_window_for_an_account_switch():
    h = member_harness()
    h.controller.open_window()
    h.controller.close_window()
    assert not h.controller.window.isVisible()


def test_an_open_window_follows_an_identity_that_arrives_another_way():
    h = Harness(roster=(STRANGER,))
    h.profile = None
    h.controller.open_window()
    window = h.controller.window
    assert window.pubkey is None
    h.profile = Profile(STRANGER)                  # connected from the Nostr menu
    h.controller.account_changed(h.profile)
    assert window.pubkey == STRANGER
    assert h.apis[-1].profile is h.profile
    assert h.apis[0].canceled == 1                 # the old client is let go
    window.close()


def test_an_open_window_for_the_same_account_is_left_alone():
    h = member_harness()
    h.controller.open_window()
    apis = len(h.apis)
    h.controller.account_changed(h.profile)
    assert len(h.apis) == apis
    h.controller.window.close()


def test_connect_signer_in_the_window_continues_as_the_new_account():
    h = Harness(roster=(STRANGER,))
    h.profile = None
    h.controller.open_window()
    window = h.controller.window

    def connected():
        h.profile = Profile(STRANGER)
    h.on_connect = connected
    window.connect_requested.emit()
    assert h.connects == 1
    assert window.pubkey == STRANGER
    window.close()


def test_a_confirmed_member_has_the_benefits_at_once():
    h = member_harness(roster=(STRANGER,), profile=Profile(MEMBER_KEY))
    assert h.controller.entitled_relays() == []
    h.controller.open_window()
    h.controller.window.member_confirmed.emit(MEMBER_KEY)
    assert h.controller.entitled_relays() == [MEMBER_RELAY]
    assert h.changes == [True]
    assert mc.RECOGNIZED_MESSAGE in h.status
    h.controller.window.close()


def test_a_saved_address_is_shown_when_the_window_opens_again():
    h = member_harness()
    h.controller.open_window()
    window = h.controller.window
    window.address_saved.emit(MEMBER_KEY, "satoshi")
    window.close()
    h.controller.open_window()
    assert h.controller.window._name_detail.text() == "satoshi@einundzwanzig.space"
    h.controller.window.close()


def test_deleted_data_drops_the_confirmation_and_refreshes_the_roster():
    h = member_harness(roster=(STRANGER,), profile=Profile(MEMBER_KEY))
    h.controller.open_window()
    window = h.controller.window
    window.member_confirmed.emit(MEMBER_KEY)
    assert h.controller.entitled_relays() == [MEMBER_RELAY]
    requested = len(h.nam.requested)
    window.data_erased.emit(MEMBER_KEY)
    assert len(h.nam.requested) == requested + 1   # fetched even though fresh
    h.answer()
    assert h.controller.entitled_relays() == []
    assert h.changes == [True, True]
    window.close()


# --------------------------------------------------------------------- #
# The members' relay in the relay list                                   #
# --------------------------------------------------------------------- #

def open_member_window(h):
    h.controller.open_window()
    return h.controller.window


@pytest.mark.parametrize("outcome,done,offered", [
    (rla.ADDED, True, False),
    (rla.ALREADY, True, False),
    (rla.NO_LIST, False, True),
    (rla.UNREADABLE, False, False),
    (rla.FAILED, False, False),
])
def test_each_relay_outcome_reaches_the_window(outcome, done, offered):
    h = member_harness()
    window = open_member_window(h)
    window._relay_button.click()
    job = h.jobs[0]
    assert job.started == 1 and job.profile is h.profile
    job.finished.emit(outcome)
    assert window._relay_result.text() == rla.outcome_message(outcome)
    assert window._relay_button.isHidden() == done
    assert (window._relay_button.text() == "Publish Recommended Relay List") == offered
    window.close()


def test_the_recommended_list_is_published_only_after_the_click():
    h = member_harness()
    window = open_member_window(h)
    window._relay_button.click()
    h.jobs[0].finished.emit(rla.NO_LIST)
    assert len(h.jobs) == 1                        # offered, not done
    window._relay_button.click()
    assert len(h.jobs) == 2 and h.jobs[1].started == 1
    h.jobs[1].finished.emit(rla.PUBLISHED)
    assert window._relay_button.isHidden()
    window.close()


def test_a_finished_relay_job_is_released():
    h = member_harness()
    window = open_member_window(h)
    window._relay_button.click()
    job = h.jobs[0]
    job.finished.emit(rla.ADDED)
    flush_deletes()
    assert not shiboken6.isValid(job)
    window.close()


# --------------------------------------------------------------------- #
# What MainWindow does with it                                           #
# --------------------------------------------------------------------- #

def self_calls(name: str):
    """Matcher for ``self.<...>.<name>(...)``."""
    def matches(node) -> bool:
        return (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == name)
    return matches


def method_body(name: str):
    source = textwrap.dedent(inspect.getsource(getattr(MainWindow, name)))
    return ast.parse(source).body[0].body


def keyword_of(body, call_name: str, keyword: str):
    """The AST node a keyword argument is given, across every such call."""
    found = []
    for statement in body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            named = getattr(func, "id", "") or getattr(func, "attr", "")
            if named != call_name:
                continue
            for kwarg in node.keywords:
                if kwarg.arg == keyword:
                    found.append(kwarg.value)
    return found


def membership_calls(name: str):
    """Matcher for ``self._membership.<name>(...)``."""
    def matches(node) -> bool:
        func = getattr(node, "func", None)
        owner = getattr(func, "value", None)
        return (isinstance(node, ast.Call) and getattr(func, "attr", "") == name
                and getattr(owner, "attr", "") == "_membership")
    return matches


def test_the_controller_is_built_and_started_after_the_media_store(init_body):
    built = statement_index(init_body, calls("MembershipController"))
    store = statement_index(init_body, calls("MediaStore"))
    started = statement_index(init_body, membership_calls("start"))
    assert 0 <= built < store < started


def test_the_media_store_is_given_the_membership_providers(init_body):
    call = constructor(init_body, "MediaStore")
    assert attribute_argument(call, "entitled_servers") == "_entitled_blossom_servers"
    assert attribute_argument(call, "server_quota") == "_entitled_quota"


@pytest.mark.parametrize("handler", [
    "_on_nostr_profile_connected",
    "_on_nostr_select_profile",
])
def test_the_controller_hears_when_the_account_changes(handler):
    body = method_body(handler)
    assert statement_index(body, membership_calls("account_changed")) >= 0


def test_an_account_switch_follows_the_teardown():
    # Updating the window before the teardown closes it would build a
    # client for a window about to go.
    body = method_body("_on_nostr_select_profile")
    teardown = statement_index(body, self_calls("_release_identity_state"))
    follow = statement_index(body, membership_calls("account_changed"))
    assert 0 <= teardown < follow


class StandIn:
    """The parts of MainWindow its membership handlers touch."""

    _on_membership_benefits_changed = MainWindow._on_membership_benefits_changed
    _visible_media_library = MainWindow._visible_media_library
    _forget_media_library = MainWindow._forget_media_library
    _entitled_relays = MainWindow._entitled_relays
    _entitled_blossom_servers = MainWindow._entitled_blossom_servers
    _entitled_quota = MainWindow._entitled_quota
    _media_server_label = MainWindow._media_server_label

    def __init__(self, dialog=None, membership=None):
        self._media_library_dialog = dialog
        self._membership = membership
        self.refetches = 0
        self.fetches = 0
        self.reroutes = 0
        stand_in = self
        self._media_store = SimpleNamespace(
            refetch_if_targets_changed=lambda: setattr(
                stand_in, "refetches", stand_in.refetches + 1),
            fetch=lambda **kw: setattr(stand_in, "fetches", stand_in.fetches + 1))
        self._draft_sync = SimpleNamespace(
            reroute=lambda: setattr(stand_in, "reroutes", stand_in.reroutes + 1))


def closed_library_dialog():
    """A dialog that deletes itself on close, the way the library does."""
    dialog = QDialog()
    dialog.setAttribute(Qt.WA_DeleteOnClose)
    dialog.show()
    dialog.close()
    flush_deletes()
    return dialog


def test_a_closed_media_library_neither_raises_nor_stops_the_draft_reroute():
    dialog = closed_library_dialog()
    assert not shiboken6.isValid(dialog)
    win = StandIn(dialog)
    win._on_membership_benefits_changed()          # used to raise every tick
    assert win.reroutes == 1
    assert win._media_library_dialog is None
    assert win.refetches == 0


def test_an_open_media_library_refetches_only_when_its_servers_changed():
    dialog = QDialog()
    dialog.show()
    win = StandIn(dialog)
    win._on_membership_benefits_changed()
    assert win.refetches == 1 and win.fetches == 0
    dialog.close()


def test_a_destroyed_library_is_forgotten_only_if_it_is_still_the_one_held():
    first, second = object(), object()
    win = StandIn(second)
    win._forget_media_library(first)
    assert win._media_library_dialog is second
    win._forget_media_library(second)
    assert win._media_library_dialog is None


def test_the_main_windows_providers_are_the_controllers():
    c = member_harness().controller
    win = StandIn(membership=c)
    assert win._entitled_relays() == [MEMBER_RELAY]
    assert win._entitled_blossom_servers() == [MEMBER_BLOSSOM]
    assert win._entitled_quota(MEMBER_BLOSSOM) == PER_USER_BYTES
    assert win._media_server_label(MEMBER_BLOSSOM) == "EINUNDZWANZIG"


# --------------------------------------------------------------------- #
# Entitled relays reach the publish paths                                #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("handler,job", [
    ("_fire_draft_publish_job", "DraftPublishJob"),
    ("_run_draft_deletion", "DraftBulkDeleteJob"),
])
def test_a_publish_job_is_given_the_resolved_relays(handler, job):
    # A job must get the list, not the provider: a callable is truthy and
    # would be published as if it were a relay URL.
    try:
        body = method_body(handler)
    except AttributeError:
        pytest.skip(f"{handler} not present under that name")
    values = keyword_of(body, job, "entitled_relays")
    assert values, f"{job} is not given entitled_relays"
    for node in values:
        assert isinstance(node, ast.Call), (
            f"{job} received the provider itself rather than its result"
        )


@pytest.mark.parametrize("handler,dialog", [
    ("_on_nostr_publish_note", "PublishNoteDialog"),
    ("_on_nostr_publish_article", "PublishArticleDialog"),
])
def test_a_publish_dialog_is_given_the_provider(handler, dialog):
    # A dialog gets the provider, so membership resolving while it is open
    # still counts by the time the user presses publish.
    try:
        body = method_body(handler)
    except AttributeError:
        pytest.skip(f"{handler} not present under that name")
    values = keyword_of(body, dialog, "entitled_relays")
    assert values, f"{dialog} is not given entitled_relays"
    for node in values:
        assert not isinstance(node, ast.Call), (
            f"{dialog} received an already-resolved list, so membership "
            f"resolving while it is open would be ignored"
        )


# --------------------------------------------------------------------- #
# Identity-scoped state is released when the account changes            #
# --------------------------------------------------------------------- #

def test_identity_teardown_names_the_drafts_the_media_keys_and_the_window():
    # The private library holds a decryption key per file, so an account
    # left behind with its keys resident is a privacy problem and not
    # just untidy. All of these belong to the same teardown.
    body = method_body("_release_identity_state")
    released = {
        node.func.value.attr
        for statement in body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", "") == "stop"
        and isinstance(getattr(node.func, "value", None), ast.Attribute)
    }
    assert "_draft_sync" in released
    assert "_private_library" in released
    assert statement_index(body, membership_calls("close_window")) >= 0
    assert "_media_store.clear()" in ast.unparse(body)


@pytest.mark.parametrize("handler", [
    "_on_nostr_select_profile",
    "_on_nostr_profile_connected",
    "_on_nostr_sign_out",
])
def test_every_identity_transition_releases_the_previous_account(handler):
    # Three separate paths change the active account. Missing any one of
    # them leaves the previous identity's keys in memory, which is the
    # defect this pins.
    body = method_body(handler)
    assert statement_index(body, self_calls("_release_identity_state")) >= 0


def test_reselecting_the_same_account_does_not_tear_it_down():
    # Re-selecting the current profile must not discard drafts already
    # decrypted, which would cost a fresh round of signer prompts for
    # nothing.
    source = textwrap.dedent(inspect.getsource(MainWindow._on_nostr_select_profile))
    assert "leaving" in source
    guarded = [
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.If)
        and any(
            isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_release_identity_state"
            for n in ast.walk(node)
        )
    ]
    assert guarded, "the teardown is unconditional, so re-selecting costs prompts"
