# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PublishJob routes a public event the NIP-65 way.

  - The author's write relays come first, then the read relays of each
    person the event mentions (or the relay hint their ``p`` tag carries
    when their list is unknown).
  - The relay plan and the signature are asked for at the same time, and
    nothing is published until both are in.
  - The author's relay list follows the event to the mentioned people's
    relays that took it.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.outbox.policy import LookupState, RelayList  # noqa: E402
from nostr.publisher import PublishJob, mentioned_pubkeys  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    PK, FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool, Profile, settle,
)

ALICE = "a" * 64
BOB = "b" * 64
MEMBER_RELAY = "wss://members.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def relay_list(write=(), read=()):
    return RelayList(write=list(write), read=list(read), state=LookupState.FOUND)


def note(*p_tags):
    return {"kind": 1, "pubkey": PK, "content": "hello", "created_at": 1_800_000_000,
            "tags": [list(tag) for tag in p_tags]}


def directory():
    return FakeRelayDirectory({
        PK: relay_list(write=["wss://w1.example", "wss://w2.example"],
                       read=["wss://my-inbox.example"]),
        ALICE: relay_list(write=["wss://alice-out.example"],
                          read=["wss://a1.example", "wss://a2.example", "wss://a3.example"]),
    })


def make_job(event, *, d=None, pool=None, session_pool=None, entitled=()):
    return PublishJob(
        relay_pool=pool or FakePool(),
        relay_directory=d or directory(),
        session_pool=session_pool or FakeSessionPool(FakeClient()),
        profile=Profile(),
        unsigned_event=event,
        entitled_relays=entitled,
    )


def run(job):
    seen = {"completed": [], "failed": []}
    job.completed.connect(seen["completed"].append)
    job.failed.connect(seen["failed"].append)
    job.start()
    settle()
    return seen


# -- where it goes ---------------------------------------------------------------------

def test_the_authors_write_relays_lead_then_the_mentioned_inboxes():
    pool = FakePool()
    event = note(["p", ALICE], ["p", BOB, "wss://bob-hint.example"])
    seen = run(make_job(event, pool=pool))
    targets, published = pool.published[0]
    assert targets == ["wss://w1.example", "wss://w2.example",
                       "wss://a1.example", "wss://bob-hint.example",   # everyone's first
                       "wss://a2.example"]
    assert published["id"] and seen["completed"] and not seen["failed"]


def test_a_members_relay_rides_behind_the_authors_own():
    pool = FakePool()
    run(make_job(note(), pool=pool, entitled=[MEMBER_RELAY]))
    assert pool.published[0][0] == ["wss://w1.example", "wss://w2.example", MEMBER_RELAY]


def test_the_directory_is_asked_about_every_mention_with_its_hint():
    d = directory()
    run(make_job(note(["p", ALICE], ["p", BOB, "wss://bob-hint.example"]), d=d))
    (_name, author, mentioned, _entitled), = d.asked("publish_plan")
    assert author == PK
    assert mentioned == ((ALICE, ""), (BOB, "wss://bob-hint.example"))


# -- sharing the author's relay list (NIP-65) ------------------------------------------

def test_the_relay_list_follows_the_note_to_the_inboxes_that_took_it():
    d = directory()
    pool = FakePool(refuse={"wss://a2.example"})
    run(make_job(note(["p", ALICE], ["p", BOB, "wss://bob-hint.example"]), d=d, pool=pool))
    assert d.shared == [(PK, ["wss://a1.example", "wss://bob-hint.example"])]


def test_a_note_mentioning_nobody_shares_nothing():
    d = directory()
    run(make_job(note(), d=d))
    assert d.shared == []


def test_nothing_is_shared_where_the_note_did_not_land():
    d = directory()
    pool = FakePool(refuse={"wss://a1.example", "wss://a2.example"})
    run(make_job(note(["p", ALICE]), d=d, pool=pool))
    assert d.shared == []


# -- the plan and the signature, side by side ---------------------------------------

class ParkedDirectory(FakeRelayDirectory):
    """Holds the plan back until the test releases it."""

    def __init__(self, lists):
        super().__init__(lists)
        self.parked = []

    def publish_plan(self, author, on_done, **kwargs):
        self.parked.append(lambda: super(ParkedDirectory, self).publish_plan(
            author, on_done, **kwargs))


class ParkedClient(FakeClient):
    """Holds each signature back until the test releases it."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.parked = []

    def sign_event(self, unsigned, on_success, on_failure, **kw):
        self.requests.append(dict(unsigned))
        self.parked.append(lambda: FakeClient.sign_event(
            self, unsigned, on_success, on_failure))


def test_the_signer_is_asked_before_the_plan_is_known():
    d = ParkedDirectory(directory().lists)
    client = ParkedClient()
    pool = FakePool()
    job = make_job(note(), d=d, pool=pool, session_pool=FakeSessionPool(client))
    job.start()
    assert len(d.parked) == 1 and len(client.requests) == 1
    client.parked.pop()()
    settle()
    assert pool.published == []                  # signed, but no plan yet
    d.parked.pop()()
    settle()
    assert pool.published[0][0][:2] == ["wss://w1.example", "wss://w2.example"]


def test_a_plan_that_lands_first_waits_for_the_signature():
    d = ParkedDirectory(directory().lists)
    client = ParkedClient()
    pool = FakePool()
    job = make_job(note(), d=d, pool=pool, session_pool=FakeSessionPool(client))
    job.start()
    d.parked.pop()()
    settle()
    assert pool.published == []
    client.parked.pop()()
    settle()
    assert len(pool.published) == 1


def test_a_refused_signature_publishes_nothing_when_the_plan_lands_later():
    d = ParkedDirectory(directory().lists)
    pool = FakePool()
    job = make_job(note(), d=d, pool=pool,
                   session_pool=FakeSessionPool(FakeClient(fail="user said no")))
    failures = []
    job.failed.connect(failures.append)
    job.start()
    d.parked.pop()()
    settle()
    assert failures == ["user said no"] and pool.published == []


# -- reading the mentions off the event ----------------------------------------------

def test_mentions_are_the_p_tags_once_each_with_their_hints():
    event = note(["p", ALICE.upper(), "wss://hint.example"], ["e", "x" * 64],
                 ["p", ALICE], ["p", "not-a-pubkey"], ["p"], ["p", BOB])
    assert mentioned_pubkeys(event) == [(ALICE, "wss://hint.example"), (BOB, "")]


# -- stopping ----------------------------------------------------------------------------

class FailingSessionPool:
    def __init__(self, reason):
        self.reason = reason

    def get(self, profile, on_ready, on_error):
        on_error(self.reason)


def watch(job):
    seen = {"completed": [], "failed": [], "signed": [], "status": []}
    job.completed.connect(seen["completed"].append)
    job.failed.connect(seen["failed"].append)
    job.signed.connect(seen["signed"].append)
    job.status_changed.connect(seen["status"].append)
    return seen


def test_a_cancelled_job_publishes_nothing_when_the_signature_lands_later():
    client = ParkedClient()
    pool = FakePool()
    job = make_job(note(["p", ALICE]), pool=pool, session_pool=FakeSessionPool(client))
    seen = watch(job)
    job.start()
    seen["status"].clear()
    job.cancel()
    client.parked.pop()()
    settle()
    assert pool.published == []
    assert seen == {"completed": [], "failed": [], "signed": [], "status": []}


def test_a_signer_that_cannot_be_reached_fails_the_job():
    pool = FakePool()
    job = make_job(note(), pool=pool,
                   session_pool=FailingSessionPool("bunker relay refused the connection"))
    seen = watch(job)
    job.start()
    settle()
    assert seen["failed"] == ["bunker relay refused the connection"]
    assert pool.published == [] and seen["completed"] == []


def _dialog(kind, tmp_path, job):
    from nostr.avatar_store import AvatarStore
    from nostr.known_people import KnownPeople
    from nostr.profiles import Profile as StoredProfile
    from nostr.profiles import ProfileStore
    from nostr.search import Nip50SearchClient
    from tests.outbox_fakes import HandPool

    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = StoredProfile(user_pubkey=PK, bunker_pubkey="b" * 64,
                            bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64)
    store.upsert(profile)
    people = KnownPeople(path=tmp_path / "people.json")
    common = dict(active_profile=profile, store=store, relay_pool=FakePool(),
                  relay_directory=directory(), session_pool=FakeSessionPool(FakeClient()),
                  known_people=people, search_client=Nip50SearchClient(HandPool(), people),
                  avatars=AvatarStore())
    if kind == "note":
        from nostr.ui.publish_note_dialog import PublishNoteDialog
        dialog = PublishNoteDialog(content="hello", **common)
    else:
        from nostr.ui.publish_article_dialog import PublishArticleDialog
        dialog = PublishArticleDialog(body_markdown="# Hello\n\nBody.", **common)
    dialog._job = job
    return dialog


class StubJob:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


@pytest.mark.parametrize("kind", ["note", "article"])
def test_closing_a_publish_dialog_stops_its_job(kind, tmp_path):
    job = StubJob()
    dialog = _dialog(kind, tmp_path, job)
    dialog._on_cancel()
    assert job.cancelled and dialog._job is None
    job = StubJob()
    dialog = _dialog(kind, tmp_path, job)
    dialog.reject()                                  # Escape, or the close button
    assert job.cancelled
