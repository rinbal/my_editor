# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins when an article says it was published (NIP-23 ``published_at``).

What must hold:

  ``published_at`` is the time of the first publication. Publishing a
  new version of an article (the same identifier) keeps it: from the
  draft the tab came from when that draft carries it, from what this
  computer remembers of its own publications, otherwise from the version
  already on the relays (those the author publishes to, reads from, and
  the indexers).

  Nothing found counts as a new article, dated now, when at least one of
  the author's write relays answered (and an indexer, where there are
  indexers): relays fail all the time, and one that is down must not
  turn every new article into a question. Only when no write relay
  answered at all is the evidence too thin; then the person decides, and
  Cancel signs nothing (review F3: an edit got today's date).

  The reviewer's two live cases (the article's only relay down while
  another of the author's relays answers; the author's relay list moved
  to relays that never had it) keep the date on the computer that
  published the article: its record answers before any relay is asked,
  and a draft saved after publishing carries the date to other devices.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.publisher import (  # noqa: E402
    FOUND, NEVER, UNKNOWN, FirstPublication, published_at_of,
)
from tests.outbox_fakes import PK, FakeClient, FakePool, FakeSessionPool  # noqa: E402

FIRST = 1_700_000_000


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


# -- reading it from a version of the article -----------------------------------------

def test_the_tag_says_when_it_was_first_published():
    assert published_at_of({"tags": [["d", "x"], ["published_at", str(FIRST)]],
                            "created_at": FIRST + 500}) == FIRST


def test_a_version_without_the_tag_dates_from_when_it_was_made():
    assert published_at_of({"tags": [["d", "x"]], "created_at": FIRST + 500}) == FIRST + 500


@pytest.mark.parametrize("event", [None, {}, {"tags": [["published_at", "soon"]]},
                                   {"tags": [], "created_at": True}])
def test_no_usable_time_is_none(event):
    assert published_at_of(event) is None


def _asking(write=("wss://nos.lol",), read=("wss://read.example",), also=()):
    """The question, asked: the test answers for each relay by hand.

    Its time never runs out on its own while a test answers (a pause in a
    slow run, such as a garbage collection, must not end the question
    first); a test of silence ends it with ``_time_runs_out``."""
    from nostr.outbox.policy import LookupState, RelayList
    from nostr.publisher import find_first_publication
    from tests.outbox_fakes import FakeRelayDirectory, HandPool, settle
    directory = FakeRelayDirectory()
    directory.set(PK, RelayList(write=list(write), read=list(read), state=LookupState.FOUND))
    pool = HandPool()
    found = []
    owner = QObject()
    find_first_publication(pool, directory, PK, "my-post", found.append, parent=owner,
                           timeout_ms=3_600_000, **({"also": also} if also else {}))
    settle()
    # One request per relay, whatever it asks about.
    assert len(pool.subs) == (2 if also else 1)
    return (_Relays(pool.subs) if also else pool.subs[0]), found, owner


class _Relays:
    """The question as the relays see it when it took a request for some
    relays and another for the others: each relay answers its own request,
    with what matches it."""

    def __init__(self, subs):
        self.subs = subs
        assert not set(subs[0].urls) & set(subs[1].urls)     # one request per relay

    @property
    def urls(self):
        return [url for sub in self.subs for url in sub.urls]

    def filter_of(self, url):
        return next(sub.filters[0] for sub in self.subs if url in sub.urls)

    def answer(self, url, *events):
        names = self.filter_of(url)["#d"]
        for sub in self.subs:
            if url in sub.urls:
                sub.answer(url, *[event for event in events
                                  if any(tag[:1] == ["d"] and tag[1] in names
                                         for tag in event["tags"])])

    def refuse(self, url, reason=""):
        for sub in self.subs:
            if url in sub.urls:
                sub.refuse(url, reason)


def _time_runs_out(owner):
    """The relays still silent never answer: the question's time is up now."""
    for timer in owner.findChildren(QTimer):
        timer.timeout.emit()


def test_the_version_on_the_relays_is_asked_for_by_its_identifier_everywhere_it_may_be():
    from nostr.outbox.defaults import INDEXER_RELAYS
    from tests.outbox_fakes import settle, signed
    sub, found, _owner = _asking()
    assert sub.filters[0]["kinds"] == [30023] and sub.filters[0]["#d"] == ["my-post"]
    # The author's relays, the ones they read from, and the indexers.
    assert {"wss://nos.lol", "wss://read.example", *INDEXER_RELAYS} <= set(sub.urls)
    for url in sub.urls:
        sub.answer(url, signed(30023, [["d", "my-post"], ["published_at", str(FIRST)]]))
    settle()
    assert found == [FirstPublication(FOUND, FIRST)]


def _outcome(write, answering, *, how="refuses"):
    from nostr.outbox.defaults import INDEXER_RELAYS
    from tests.outbox_fakes import settle
    sub, found, owner = _asking(write=write)
    for url in sub.urls:
        if url in answering or (url in INDEXER_RELAYS and "indexer" in answering):
            sub.answer(url)                    # nothing stored
        elif how == "refuses":
            sub.refuse(url, "blocked: maintenance")
        # else: silent until the time is up
    if how == "times out":
        _time_runs_out(owner)
    settle()
    return found


@pytest.mark.parametrize("how", ["refuses", "times out"])
def test_one_dead_relay_does_not_make_a_new_article_a_question(how):
    # The coordinator's decision: relays die all the time.
    found = _outcome(("wss://nos.lol", "wss://down.example"), {"wss://nos.lol", "indexer"},
                     how=how)
    assert found == [FirstPublication(NEVER)]


@pytest.mark.parametrize("how", ["refuses", "times out"])
def test_with_no_write_relay_answering_nobody_can_tell(how):
    found = _outcome(("wss://down.example", "wss://gone.example"),
                     {"wss://read.example", "indexer"}, how=how)
    assert found == [FirstPublication(UNKNOWN)]


def test_without_an_indexer_answering_the_evidence_is_too_thin():
    found = _outcome(("wss://nos.lol",), {"wss://nos.lol"})
    assert found == [FirstPublication(UNKNOWN)]


def test_a_version_found_anywhere_gives_the_date():
    from tests.outbox_fakes import settle, signed
    sub, found, _owner = _asking(write=("wss://moved.example", "wss://down.example"))
    for url in sub.urls:
        if url == "wss://read.example":
            sub.answer(url, signed(30023, [["d", "my-post"], ["published_at", str(FIRST)]]))
        else:
            sub.refuse(url)
    settle()
    assert found == [FirstPublication(FOUND, FIRST)]


# -- a file's own names, asked about with the made one ----------------------------------

# "Grüße an alle" as a Mac writes a file's name ("u" and an accent), and composed.
RAW = "Gru\u0308\u00dfe an alle"
COMPOSED = "Gr\u00fc\u00dfe an alle"


def test_a_files_own_names_are_asked_about_with_the_made_one_in_one_question():
    """Review round 3: 3.3 published a file's article under the file's
    name as it was (on a Mac, decomposed); one written elsewhere may be
    composed. Both are asked about with the made link name, in one
    request, and one found is the article's, exactly as it is."""
    from tests.outbox_fakes import settle, signed
    sub, found, _owner = _asking(also=(RAW, COMPOSED))
    assert sub.filter_of("wss://nos.lol")["#d"] == [RAW, COMPOSED, "my-post"]
    for url in sub.urls:
        if url == "wss://nos.lol":
            sub.answer(url, signed(30023, [["client", "MyEditor"], ["d", RAW],
                                           ["published_at", str(FIRST)]]))
        else:
            sub.answer(url)
    settle()
    assert found == [FirstPublication(FOUND, FIRST, under=RAW)]


@pytest.mark.parametrize("names, under", [
    ((RAW, COMPOSED), RAW),             # the name as the file system gives it first
    ((COMPOSED, "my-post"), COMPOSED),  # a file's own name before the made one
    (("my-post",), ""),                 # under the made name: no other name
])
def test_found_under_more_than_one_name_the_files_own_comes_first(names, under):
    from tests.outbox_fakes import settle, signed
    sub, found, _owner = _asking(also=(RAW, COMPOSED))
    for url in sub.urls:
        sub.answer(url, *[signed(30023, [["d", name], ["published_at", str(FIRST + i)]])
                          for i, name in enumerate(names)])
    settle()
    assert found[0].state == FOUND and found[0].under == under
    assert found[0].published_at == FIRST + names.index(under or "my-post")


def test_a_files_own_names_go_only_to_the_relays_it_was_published_to():
    """Review round 4, L2: the file's own names were asked of the author's
    read relays and the public indexers too, also when the article would
    not go out under them. 3.3 published to the write relays: only those
    are asked about the file's names; the others about the made name, as
    for any article."""
    from nostr.outbox.defaults import INDEXER_RELAYS
    relays, _found, _owner = _asking(also=(RAW, COMPOSED))
    assert {"wss://read.example", *INDEXER_RELAYS} <= set(relays.urls)
    for url in relays.urls:
        assert relays.filter_of(url)["#d"] == (
            [RAW, COMPOSED, "my-post"] if url == "wss://nos.lol" else ["my-post"])


@pytest.mark.parametrize("answering, state", [({"wss://nos.lol", "indexer"}, NEVER),
                                              ({"wss://nos.lol"}, UNKNOWN)])
def test_with_none_of_the_names_found_it_is_as_for_one(answering, state):
    from nostr.outbox.defaults import INDEXER_RELAYS
    from tests.outbox_fakes import settle
    sub, found, _owner = _asking(also=(RAW, COMPOSED))
    for url in sub.urls:
        if url in answering or (url in INDEXER_RELAYS and "indexer" in answering):
            sub.answer(url)
        else:
            sub.refuse(url, "blocked")
    settle()
    assert found == [FirstPublication(state)]


# -- the publish dialog ---------------------------------------------------------------

class CapturedJob(QObject):
    """Stands in for PublishJob: keeps the event it was given."""

    status_changed = Signal(str)
    signed = Signal(str)
    completed = Signal(list)
    failed = Signal(str)
    made = []

    def __init__(self, **kwargs):
        super().__init__(kwargs.get("parent"))
        self.event = kwargs["unsigned_event"]
        CapturedJob.made.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture
def captured(monkeypatch):
    import nostr.ui.publish_article_dialog as module
    CapturedJob.made = []
    monkeypatch.setattr(module, "PublishJob", CapturedJob)
    return CapturedJob.made


def _dialog(tmp_path, *, slug="my-post", first_published=None, lookup=None,
            first_publications=None, **options):
    from nostr.avatar_store import AvatarStore
    from nostr.known_people import KnownPeople
    from nostr.profiles import Profile as StoredProfile
    from nostr.profiles import ProfileStore
    from nostr.search import Nip50SearchClient
    from nostr.ui.publish_article_dialog import PublishArticleDialog
    from tests.outbox_fakes import FakeRelayDirectory, HandPool

    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = StoredProfile(user_pubkey=PK, bunker_pubkey="b" * 64,
                            bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64)
    store.upsert(profile)
    people = KnownPeople(path=tmp_path / "people.json")
    return PublishArticleDialog(
        body_markdown="# Hello\n\nBody.", active_profile=profile, store=store,
        relay_pool=FakePool(), relay_directory=FakeRelayDirectory(),
        session_pool=FakeSessionPool(FakeClient()), known_people=people,
        search_client=Nip50SearchClient(HandPool(), people), avatars=AvatarStore(),
        default_title="Hello", default_slug=slug, first_published=first_published,
        first_publications=first_publications, published_at_lookup=lookup, **options)


def _published_at(job) -> int:
    return int(next(t[1] for t in job.event["tags"] if t[0] == "published_at"))


def test_a_draft_that_knows_its_first_publication_keeps_it(tmp_path, captured):
    asked = []
    dialog = _dialog(tmp_path, first_published=FIRST,
                     lookup=lambda *args: asked.append(args))
    dialog._on_publish()
    assert asked == []                               # nothing to look up
    assert _published_at(captured[0]) == FIRST


def test_an_edit_keeps_the_date_of_the_version_on_the_relays(tmp_path, captured):
    asked = []
    dialog = _dialog(tmp_path, lookup=lambda author, slug, done: asked.append(
        (author, slug, done)))
    dialog._on_publish()
    assert captured == [] and not dialog._publish_btn.isEnabled()   # asking first
    author, slug, done = asked[0]
    assert (author, slug) == (PK, "my-post")
    done(FirstPublication(FOUND, FIRST))
    assert _published_at(captured[0]) == FIRST


def test_a_first_publication_is_dated_now(tmp_path, captured):
    dialog = _dialog(tmp_path, lookup=lambda author, slug, done: done(
        FirstPublication(NEVER)))
    before = int(time.time())
    dialog._on_publish()
    assert before <= _published_at(captured[0]) <= int(time.time())


def test_another_identifier_does_not_take_the_drafts_date(tmp_path, captured):
    asked = []
    dialog = _dialog(tmp_path, first_published=FIRST,
                     lookup=lambda author, slug, done: (asked.append(slug),
                                                        done(FirstPublication(NEVER))))
    dialog._slug_edit.setText("a-new-post")
    dialog._on_publish()
    assert asked == ["a-new-post"]
    assert _published_at(captured[0]) > FIRST


def test_an_answer_after_closing_publishes_nothing(tmp_path, captured):
    asked = []
    dialog = _dialog(tmp_path, lookup=lambda author, slug, done: asked.append(done))
    dialog._on_publish()
    dialog.reject()
    asked[0](FirstPublication(FOUND, FIRST))
    assert captured == []


@pytest.mark.parametrize("answer", ["cancel", "new", "again"])
def test_when_nobody_can_tell_the_person_decides(tmp_path, captured, monkeypatch, answer):
    import nostr.ui.publish_article_dialog as module
    alerts = []
    monkeypatch.setattr(module, "ask", lambda *a, **k: alerts.append(k["title"]) or answer)
    asked = []

    def lookup(author, slug, done):
        asked.append(slug)
        done(FirstPublication(UNKNOWN) if len(asked) == 1 else FirstPublication(FOUND, FIRST))

    dialog = _dialog(tmp_path, lookup=lookup)
    before = int(time.time())
    dialog._on_publish()
    assert alerts == ["Couldn't check whether this article was published before"]
    if answer == "cancel":
        assert captured == [] and dialog._publish_btn.isEnabled()      # nothing signed
    elif answer == "new":
        assert before <= _published_at(captured[0]) <= int(time.time())
    else:
        assert asked == ["my-post", "my-post"] and _published_at(captured[0]) == FIRST


def test_this_computer_remembers_its_own_first_publications(tmp_path, captured):
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    dialog = _dialog(tmp_path, lookup=lambda author, slug, done: done(
        FirstPublication(NEVER)), first_publications=record)
    dialog._on_publish()
    when = _published_at(captured[0])
    dialog._on_completed([("wss://nos.lol", True, "")])
    assert FirstPublications(tmp_path / "first.json").get(PK, "my-post") == when
    # The next edit, with the relays unable to tell, keeps that date unasked.
    asked = []
    again = _dialog(tmp_path, lookup=lambda *args: asked.append(args),
                    first_publications=FirstPublications(tmp_path / "first.json"))
    again._on_publish()
    assert asked == [] and _published_at(captured[1]) == when


def test_the_publishing_computer_keeps_the_date_in_the_reviewers_cases(tmp_path, captured):
    # The reviewer's two cases (the article's relay is down while another
    # answers; the relay list moved to relays that never had it) both make
    # the relays look as if the article were new. The computer that
    # published it answers from its record, before any relay is asked, so
    # one test covers both: the lookup is never called.
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    record.remember(PK, "my-post", FIRST)
    asked = []
    dialog = _dialog(tmp_path, first_publications=record,
                     lookup=lambda *args: asked.append(args))
    dialog._on_publish()
    assert asked == [] and _published_at(captured[0]) == FIRST


def test_a_draft_saved_after_publishing_carries_the_date(tmp_path):
    # So another device that opens the draft keeps it too.
    import types
    from main_window import DraftBinding, MainWindow
    from nostr.drafts import INNER_KIND_LONG_FORM
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    record.remember(PK, "my-post", FIRST)
    window = types.SimpleNamespace(
        _draft_store=types.SimpleNamespace(get=lambda _d: types.SimpleNamespace(
            inner_tags=[["d", "my-post"], ["title", "Hello"]])),
        _first_publications=record,
        _profile_store=types.SimpleNamespace(
            default=lambda: types.SimpleNamespace(user_pubkey=PK)))
    ed = types.SimpleNamespace(_draft_binding=DraftBinding(
        identifier="my-post", inner_kind=INNER_KIND_LONG_FORM, title="Hello"))
    details = MainWindow._article_details_of(window, ed)
    assert details.published_at == FIRST
    assert ["published_at", str(FIRST)] in details.draft_tags("Body")


def test_the_record_keeps_the_earliest_date(tmp_path):
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    record.remember(PK, "post", FIRST + 10)
    record.remember(PK, "post", FIRST)
    record.remember(PK, "post", FIRST + 20)
    assert FirstPublications(tmp_path / "first.json").get(PK.upper(), "post") == FIRST
    assert record.get(PK, "other") is None
