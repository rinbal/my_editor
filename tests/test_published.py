# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an account published, read back from its relays (nostr/published.py).

What must hold: the list shows the newest version of each article and the
newest notes, newest first by first publication; whatever a deletion
request by the author covers is left out; notes come a page at a time and
a page never opens a gap; when no relay answers, the list says so instead
of looking empty; an account change drops late answers; deleting hides an
item once one relay took the request, and an item no relay took stays,
with Try Again.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr import bech32, events, published  # noqa: E402
from nostr.deletion import address, build_deletion  # noqa: E402
from nostr.published import (  # noqa: E402
    DELETING, LOADING, NOT_DELETED, PAGE_SIZE, READY, SIGNED_OUT, UNREACHABLE,
    PublishedItem, PublishedList, deletion_requests,
)
from nostr.queries import Fetched, page_horizon  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    NOW, OTHER_PK, OTHER_SK, PK, SK, FakeClient, FakeRelayDirectory, FakeSessionPool,
    Profile, settle, signed,
)
from tests.published_fakes import Relays  # noqa: E402

R1, R2 = "wss://one.example", "wss://two.example"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication(sys.argv)


def note(text, *, at=NOW - 1000, sk=SK):
    return signed(1, [["client", "MyEditor"]], text, sk=sk, created_at=at)


def article(d, title, *, at=NOW - 1000, published=None, sk=SK, body="Body"):
    tags = [["d", d], ["title", title]]
    if published is not None:
        tags.append(["published_at", str(published)])
    return signed(30023, tags, body, sk=sk, created_at=at)


def deletion(*, ids=(), addresses=(), at=NOW, sk=SK, kinds=(1,)):
    pubkey = PK if sk is SK else OTHER_PK
    return events.sign_event(build_deletion(pubkey, addresses=addresses, event_ids=ids,
                                            kinds=kinds, created_at=at), sk)


def setup(*, client=None, lists=None, refuse_publish=()):
    relays = Relays(refuse_publish=refuse_publish)
    directory = FakeRelayDirectory(lists if lists is not None else {PK: [R1, R2]})
    model = PublishedList(relay_pool=relays, relay_directory=directory,
                          session_pool=FakeSessionPool(client or FakeClient()),
                          clock=lambda: NOW)
    seen = {"done": [], "not_taken": [], "refused": [], "status": []}
    model.deletion_done.connect(lambda item, results: seen["done"].append((item.id, results)))
    model.deletion_not_taken.connect(
        lambda item, results: seen["not_taken"].append((item.id, results)))
    model.deletion_refused.connect(
        lambda item, _words, why: seen["refused"].append((item.id, why)))
    model.deletion_status.connect(lambda item, text: seen["status"].append(text))
    return model, relays, seen


def ids(model):
    return [item.id for item in model.items()]


# -- reading ------------------------------------------------------------------------------

def test_the_newest_version_of_each_article_and_the_notes_newest_first():
    model, relays, _seen = setup()
    old = article("essay", "First title", at=NOW - 5000, published=NOW - 5000)
    edited = article("essay", "Edited title", at=NOW - 100, published=NOW - 5000)
    other = article("diary", "Diary", at=NOW - 3000)
    hello, later = note("Hello\nsecond line", at=NOW - 4000), note("Later", at=NOW - 2000)
    relays.keep(R1, old, hello, other)
    relays.keep(R2, edited, later, hello)
    model.set_account(Profile())
    assert model.state == LOADING
    relays.run()
    assert model.state == READY and not model.partial
    # By first publication: the edited essay keeps its first date.
    assert ids(model) == [later["id"], other["id"], hello["id"], edited["id"]]
    essay = model.item(edited["id"])
    assert essay.title == "Edited title" and essay.first_published == NOW - 5000
    assert essay.found_on == (R1, R2)              # every relay with any version
    assert model.item(hello["id"]).title == "Hello" and model.item(hello["id"]).found_on == (R1, R2)
    assert not model.has_more


def test_what_a_deletion_request_by_the_author_covers_is_left_out():
    model, relays, _seen = setup()
    gone_note, kept_note = note("gone", at=NOW - 300), note("kept", at=NOW - 200)
    gone_article = article("gone", "Gone", at=NOW - 400)
    back_again = article("back", "Back again", at=NOW + 50)    # published after its deletion
    theirs_said = note("only the author can delete this", at=NOW - 100)
    relays.keep(R1, gone_note, kept_note, gone_article, back_again, theirs_said)
    relays.keep(R2, deletion(ids=[gone_note["id"]]),
                deletion(addresses=[address(30023, PK, "gone"), address(30023, PK, "back")],
                         kinds=[30023]),
                deletion(ids=[theirs_said["id"]], sk=OTHER_SK))
    model.set_account(Profile())
    relays.run()
    assert set(ids(model)) == {kept_note["id"], back_again["id"], theirs_said["id"]}


def test_deletion_requests_are_asked_for_by_id_and_by_address():
    model, relays, _seen = setup()
    relays.keep(R1, note("a note"), article("essay", "Essay"))
    model.set_account(Profile())
    relays.run()
    asked = [flt for sub in relays.subs for flt in sub.filters if flt.get("kinds") == [5]]
    assert any("#e" in flt for flt in asked)
    assert {"kinds": [5], "authors": [PK], "#a": [address(30023, PK, "essay")]} in asked


def test_no_relay_answering_says_so_instead_of_looking_empty():
    model, relays, _seen = setup()
    relays.down = {R1, R2}
    model.set_account(Profile())
    relays.run()
    assert model.state == UNREACHABLE and model.items() == []
    relays.down = set()
    relays.keep(R1, note("there after all"))
    model.refresh()
    assert model.state == LOADING
    relays.run()
    assert model.state == READY and len(model.items()) == 1


def test_unreadable_deletions_are_unreachable_too():
    model, relays, _seen = setup()
    relays.keep(R1, note("maybe deleted"))
    relays.refusing_kinds = {5}
    model.set_account(Profile())
    relays.run()
    assert model.state == UNREACHABLE and model.items() == []


def test_one_relay_answering_is_enough_and_says_something_may_be_missing():
    model, relays, _seen = setup()
    relays.keep(R1, note("one"))
    relays.down = {R2}
    model.set_account(Profile())
    relays.run()
    assert model.state == READY and len(model.items()) == 1 and model.partial


def test_nothing_published_is_an_empty_list():
    model, relays, _seen = setup()
    model.set_account(Profile())
    relays.run()
    assert model.state == READY and model.items() == [] and not model.has_more


def test_notes_come_a_page_at_a_time():
    model, relays, _seen = setup()
    many = [note(f"note {n}", at=NOW - n * 10) for n in range(PAGE_SIZE + 10)]
    relays.keep(R1, *many)
    model.set_account(Profile())
    relays.run()
    assert ids(model) == [n["id"] for n in many[:PAGE_SIZE]]
    assert model.has_more
    model.load_more()
    assert model.loading_more
    relays.run()
    assert ids(model) == [n["id"] for n in many]
    assert not model.has_more and not model.loading_more
    paged = [flt for sub in relays.subs for flt in sub.filters if "until" in flt]
    assert paged == [{"kinds": [1], "authors": [PK], "limit": PAGE_SIZE,
                      "until": NOW - (PAGE_SIZE - 1) * 10}]


def test_a_page_never_opens_a_gap_between_relays():
    model, relays, _seen = setup()
    recent = [note(f"recent {n}", at=NOW - n) for n in range(PAGE_SIZE)]     # one full page
    old = [note("old", at=NOW - 90_000), note("older", at=NOW - 95_000)]
    relays.keep(R1, *recent)
    relays.keep(R2, *old)
    model.set_account(Profile())
    relays.run()
    # R1 may have more from before its oldest note: R2's old notes wait.
    assert ids(model) == [n["id"] for n in recent] and model.has_more
    model.load_more()
    relays.run()
    assert ids(model) == [n["id"] for n in recent + old] and not model.has_more


def test_a_page_full_of_one_second_does_not_ask_forever():
    model, relays, _seen = setup()
    relays.keep(R1, *[note(f"burst {n}", at=NOW - 10) for n in range(PAGE_SIZE + 5)])
    model.set_account(Profile())
    relays.run()
    assert model.has_more
    model.load_more()
    relays.run()
    # The same second again, then a page from the second before it (the
    # list goes on by itself after a page that showed nothing new).
    assert not model.has_more
    asked = [flt["until"] for sub in relays.subs for flt in sub.filters if "until" in flt]
    assert asked == [NOW - 10, NOW - 11]


def test_load_more_that_reaches_no_relay_keeps_the_list_and_says_so():
    model, relays, _seen = setup()
    relays.keep(R1, *[note(f"note {n}", at=NOW - n) for n in range(PAGE_SIZE + 1)])
    model.set_account(Profile())
    relays.run()
    relays.down = {R1, R2}
    model.load_more()
    relays.run()
    assert model.more_failed and model.has_more and len(model.items()) == PAGE_SIZE


def test_a_relay_with_older_notes_that_does_not_answer_load_more_is_not_nothing_older():
    """Review F1: R1 sent a full page, so it has older notes; when it does
    not answer Load More, the list must not decide there is nothing older."""
    model, relays, _seen = setup()
    many = [note(f"note {n}", at=NOW - 10 * n) for n in range(120)]
    relays.keep(R1, *many)
    relays.keep(R2, *many[:10])
    model.set_account(Profile())
    relays.run()
    assert len(model.items()) == PAGE_SIZE and model.has_more
    relays.down = {R1}                              # R2 answers, R1 does not
    model.load_more()
    relays.run()
    assert model.has_more and model.more_failed and len(model.items()) == PAGE_SIZE
    relays.down = set()
    model.load_more()                               # Try Again
    relays.run()
    # A page starts at the horizon itself, so pages share one note.
    assert not model.more_failed and len(model.items()) == 2 * PAGE_SIZE - 1
    model.load_more()
    relays.run()
    assert len(model.items()) == 120 and not model.has_more


def test_a_relay_that_never_answered_does_not_block_load_more():
    model, relays, _seen = setup()
    relays.keep(R1, *[note(f"note {n}", at=NOW - n) for n in range(PAGE_SIZE + 5)])
    relays.down = {R2}                              # down from the start
    model.set_account(Profile())
    relays.run()
    assert model.partial and model.has_more
    model.load_more()
    relays.run()
    assert len(model.items()) == PAGE_SIZE + 5 and not model.more_failed


def test_a_first_page_whose_notes_were_all_deleted_reads_on_by_itself():
    """Review F6: a relay keeping deleted notes made the first page all
    deleted, and the list said "Nothing published yet" while older notes
    were there."""
    model, relays, _seen = setup()
    many = [note(f"note {n}", at=NOW - 10 * n) for n in range(60)]
    relays.keep(R1, *many,
                deletion(ids=[n["id"] for n in many[:PAGE_SIZE]], kinds=(1,)))
    model.set_account(Profile())
    relays.run()
    assert [i.id for i in model.items()] == [n["id"] for n in many[PAGE_SIZE:]]
    assert not model.has_more


def test_reading_on_by_itself_stops_after_a_few_pages():
    model, relays, _seen = setup()
    many = [note(f"note {n}", at=NOW - 10 * n) for n in range(PAGE_SIZE * 6)]
    relays.keep(R1, *many, deletion(ids=[n["id"] for n in many], kinds=(1,)))
    model.set_account(Profile())
    relays.run()
    paged = [flt for sub in relays.subs for flt in sub.filters if "until" in flt]
    assert len(paged) == published.AUTO_PAGES
    assert model.items() == [] and model.has_more         # Load More stays on offer


def test_a_relay_that_did_not_answer_about_deletions_means_something_may_be_missing():
    """Review F2: R2 keeps only the request and does not answer the
    deletion question; R1 ignores deletions and still has the note."""
    model, relays, _seen = setup()
    taken_back = note("taken back elsewhere", at=NOW - 500)
    relays.keep(R1, taken_back)
    relays.keep(R2, deletion(ids=[taken_back["id"]], at=NOW - 100))
    relays.unreachable_for = {R2: {5}}              # R2 answers the items only
    model.set_account(Profile())
    relays.run()
    assert model.state == READY and [i.id for i in model.items()] == [taken_back["id"]]
    assert model.partial                            # "something may be missing" is said


def test_an_answer_cut_off_by_a_flooding_relay_means_something_may_be_missing(monkeypatch):
    from nostr import queries
    monkeypatch.setattr(queries, "MAX_COPIES", 1)
    model, relays, _seen = setup()
    relays.keep(R1, note("one", at=NOW - 2), note("two", at=NOW - 1))
    model.set_account(Profile())
    relays.run()
    assert model.state == READY and model.partial


def test_a_refresh_keeps_what_is_shown_until_its_answers_are_in():
    model, relays, _seen = setup()
    first = note("first")
    relays.keep(R1, first)
    model.set_account(Profile())
    relays.run()
    relays.down = {R1, R2}
    model.refresh()
    assert model.refreshing and ids(model) == [first["id"]]
    relays.run()
    assert model.refresh_failed and model.state == READY and ids(model) == [first["id"]]


def test_an_account_change_drops_late_answers():
    model, relays, _seen = setup(lists={PK: [R1], OTHER_PK: [R2]})
    relays.keep(R1, note("Alice's"))
    relays.keep(R2, note("Bob's", sk=OTHER_SK))
    model.set_account(Profile())
    settle()
    assert relays.subs and relays.subs[0].urls == [R1]     # Alice's relays are asked
    model.set_account(Profile(OTHER_PK))                     # before they answered
    relays.run()
    assert [item.pubkey for item in model.items()] == [OTHER_PK]
    model.set_account(None)
    assert model.state == SIGNED_OUT and model.items() == []


def test_the_same_account_again_changes_nothing():
    model, relays, _seen = setup()
    relays.keep(R1, note("one"))
    model.set_account(Profile())
    relays.run()
    asked = len(relays.subs)
    model.set_account(Profile())
    relays.run()
    assert len(relays.subs) == asked and len(model.items()) == 1


# -- deleting -----------------------------------------------------------------------------

def test_deleting_hides_the_item_once_a_relay_took_the_request():
    model, relays, seen = setup()
    essay = article("essay", "Essay")
    relays.keep(R2, essay)
    model.set_account(Profile())
    relays.run()
    model.delete(essay["id"])
    assert model.deletion_state(essay["id"]) == DELETING and model.item(essay["id"])
    relays.run()
    assert model.items() == [] and model.deletion_state(essay["id"]) == ""
    (sent_to, request), = relays.published
    assert sent_to[0] == R2                       # where it was found first
    assert set(sent_to) == {R1, R2}
    assert ["a", address(30023, PK, "essay")] in request["tags"]
    assert ["e", essay["id"]] in request["tags"] and ["k", "30023"] in request["tags"]
    assert seen["done"] and seen["done"][0][0] == essay["id"]
    assert seen["status"]                          # the signer step is narrated


def test_a_deletion_no_relay_took_stays_and_try_again_sends_the_same_request():
    model, relays, seen = setup(refuse_publish={R1, R2})
    hello = note("hello")
    relays.keep(R1, hello)
    model.set_account(Profile())
    relays.run()
    model.delete(hello["id"])
    relays.run()
    assert model.item(hello["id"]) and model.deletion_state(hello["id"]) == NOT_DELETED
    assert seen["not_taken"][0][0] == hello["id"]
    relays.publisher.refuse = set()
    model.try_again(hello["id"])
    relays.run()
    assert model.items() == []
    assert relays.published[0][1] == relays.published[1][1]     # no second signature


def test_a_deletion_the_signer_refused_asks_the_signer_again_on_try_again():
    client = FakeClient(fail="user rejected")
    model, relays, seen = setup(client=client)
    hello = note("hello")
    relays.keep(R1, hello)
    model.set_account(Profile())
    relays.run()
    model.delete(hello["id"])
    relays.run()
    assert seen["refused"] == [(hello["id"], "user rejected")] and relays.published == []
    client.fail = None
    model.try_again(hello["id"])
    relays.run()
    assert model.items() == [] and len(client.requests) == 2


def test_dismissing_a_deletion_that_did_not_go_through_leaves_the_item():
    model, relays, _seen = setup(refuse_publish={R1, R2})
    hello = note("hello")
    relays.keep(R1, hello)
    model.set_account(Profile())
    relays.run()
    model.delete(hello["id"])
    relays.run()
    model.dismiss(hello["id"])
    assert model.deletion_state(hello["id"]) == "" and model.item(hello["id"])


def test_an_item_taken_back_stays_hidden_where_a_relay_missed_the_request():
    model, relays, _seen = setup()
    hello = note("hello")
    relays.keep(R1, hello)
    model.set_account(Profile())
    relays.run()
    model.delete(hello["id"])
    relays.run()
    model.refresh()                                # R1 still has it, and no request
    relays.run()
    assert model.items() == []


def test_an_account_change_lets_go_of_a_deletion_on_its_way():
    model, relays, seen = setup(lists={PK: [R1], OTHER_PK: [R2]})
    hello = note("hello")
    relays.keep(R1, hello)
    model.set_account(Profile())
    relays.run()
    model.delete(hello["id"])
    model.set_account(Profile(OTHER_PK))
    relays.run()
    assert seen["done"] == [] and model.deletion_state(hello["id"]) == ""
    assert relays.published == []


def test_a_job_let_go_of_is_deleted_once_even_when_the_list_goes_with_it(monkeypatch):
    made = []

    class Recorded(published.DeletionJob):
        def __init__(self, **kw):
            super().__init__(**kw)
            made.append(self)

    monkeypatch.setattr(published, "DeletionJob", Recorded)
    model, relays, _seen = setup(client=FakeClient(fail="user rejected"))
    hello = note("hello")
    relays.keep(R1, hello)
    model.set_account(Profile())
    relays.run()
    model.delete(hello["id"])                      # refused at once: the job is let go of
    (job,) = made
    gone = []
    job.destroyed.connect(lambda *_args: gone.append(True))
    assert job.parent() is None
    del model                                      # only the job's connections hold the list
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert gone == [True]


# -- items and rules ----------------------------------------------------------------------

@pytest.mark.parametrize("body, title", [
    ("![cover](https://example.com/c.png)\nHello **world**", "Hello world"),
    ("> a quote I liked\nmy thoughts", "a quote I liked"),
    ("**Bold opener** and [a link](https://example.com)", "Bold opener and a link"),
    ("- first point\n- second point", "first point"),
    ("## Heading of a note\ntext", "Heading of a note"),
    ("  \n  first   words \nsecond", "first words"),
    ("", ""),
])
def test_a_title_is_words_never_markdown(body, title):
    """Review F7: the app's one rule for a body without a title
    (drafts.derive_title_from_markdown), for notes and for articles
    without a title of their own."""
    assert PublishedItem(note(body)).title == title
    assert PublishedItem(signed(30023, [["d", "x"]], body)).title == title


def test_an_articles_own_title_comes_first():
    assert PublishedItem(article("d", "  A   title ")).title == "A title"


def test_a_long_first_line_is_cut_at_a_word():
    title = PublishedItem(note("word " * 60)).title
    assert len(title) <= 90 and title.endswith("…")


def test_links_lead_to_the_item_and_name_where_it_is():
    essay = PublishedItem(article("essay", "Essay"), found_on=(R1, R2, "wss://three.example"))
    entity = essay.nostr_address()
    assert bech32.decode_naddr(entity) == ("essay", PK, 30023, [R1, R2])
    hello = PublishedItem(note("hello"), found_on=(R1,))
    event_id, hints, author, kind = bech32.decode_nevent(hello.nostr_address())
    assert (event_id, hints, author, kind) == (hello.id, [R1], PK, 1)


def test_an_identifier_too_long_for_a_link_has_no_link():
    """Review F17: NIP-19 holds 255 bytes per value; such an article has
    no address to open or copy, and asking for one must not raise."""
    long_d = PublishedItem(article("ü" * 130, "Long"), found_on=(R1,))
    assert long_d.nostr_address() is None


def test_the_first_publication_of_an_article_survives_edits():
    edited = PublishedItem(article("d", "T", at=NOW, published=NOW - 9000))
    assert edited.first_published == NOW - 9000
    assert PublishedItem(article("d", "T", at=NOW)).first_published == NOW


def test_a_first_publication_after_the_version_itself_gives_way_to_its_time():
    """Review F10: a version cannot have gone out first after it was made;
    a published_at in milliseconds sorted the article above everything
    with a date thousands of years ahead."""
    milliseconds = PublishedItem(article("d", "T", at=NOW, published=NOW * 1000))
    assert milliseconds.first_published == NOW


def test_the_horizon_is_where_every_relay_is_complete():
    a = [{"id": f"a{n}", "created_at": 100 - n} for n in range(3)]
    b = [{"id": f"b{n}", "created_at": 100 - 10 * n} for n in range(3)]
    fetched = Fetched(seen_on={**{e["id"]: ("wss://a",) for e in a},
                               **{e["id"]: ("wss://b",) for e in b}})
    assert page_horizon(fetched, a + b, 3) == 98
    assert page_horizon(fetched, a + b[:2], 3) == 98
    assert page_horizon(fetched, a[:2] + b[:2], 3) is None


def test_deletion_requests_are_asked_in_bounded_pieces():
    requests = deletion_requests(PK, [f"{n:064x}" for n in range(250)],
                                 [address(30023, PK, f"d{n}") for n in range(150)])
    filters = [flt for request in requests for flt in request]
    assert [len(flt.get("#e", flt.get("#a", []))) for flt in filters] == [100, 100, 50, 100, 50]
    assert [len(request) for request in requests] == [4, 1]
    assert deletion_requests(PK, [], []) == []


# -- curations ----------------------------------------------------------------------------

def curation(d, title, *, at=NOW - 1500, articles=("30023:" + "b" * 64 + ":one",)):
    tags = [["d", d], ["title", title], ["description", "Reading for the weekend"]]
    tags += [["a", ref] for ref in articles]
    return signed(30004, tags, "", created_at=at)


def test_curations_are_read_beside_articles_and_notes():
    """A writer's curations (Standup sites and magazines, kind 30004) are
    part of what they published: listed, addressable, and kept apart from
    an article that happens to share their identifier."""
    model, relays, _seen = setup()
    shelf = curation("shelf", "Weekend reading")
    same_d = article("shelf", "An article with the same identifier", at=NOW - 1200)
    relays.keep(R1, shelf, same_d, note("Hello"))
    model.set_account(Profile())
    relays.run()
    items = {item.title: item for item in model.items()}
    assert set(items) == {"Weekend reading", "An article with the same identifier", "Hello"}
    shelf_item = items["Weekend reading"]
    assert shelf_item.is_curation and not shelf_item.is_article
    assert shelf_item.identifier == "shelf"
    assert shelf_item.curated == 1
    assert bech32.decode_naddr(shelf_item.nostr_address())[:3] == ("shelf", PK, 30004)


def test_a_deleted_curation_stays_out():
    model, relays, _seen = setup()
    relays.keep(R1, curation("gone", "Gone"),
                deletion(addresses=[address(30004, PK, "gone")], kinds=(30004,)))
    model.set_account(Profile())
    relays.run()
    assert model.items() == []
