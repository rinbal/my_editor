# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins an article's details on their way through drafts and publishing
(nostr/article_details.py).

What must hold:

  An article draft's details (identifier, title, summary, cover,
  hashtags, first publication date, and the source and picture tags
  MyEditor carries without editing) survive every save of the draft:
  review F4 found that saving an imported draft kept only its
  identifier, title and summary, so publishing it then re-dated it and
  lost its cover and hashtags.

  Saved under another identifier, the draft is another article and has
  no first publication date of its own yet.

  The publish dialog offers the draft's details and publishes them,
  with the source; the identifier stays when the title is changed.

  Tags another app gave an article (zap splits, a content warning, alt
  text, labels) are carried when it is edited and published here; only
  the tags MyEditor writes itself are made again.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from nostr.article_details import ArticleDetails  # noqa: E402

FIRST = 1_600_000_000
PK = "ab" * 32
COVER = "https://cdn.example/cover.jpg"
PICTURE = "https://cdn.example/inline.png"
IMPORTED_TAGS = [
    ["client", "MyEditor"], ["d", "imported-post"], ["title", "Imported"],
    ["summary", "What it is about"], ["image", COVER], ["published_at", str(FIRST)],
    ["t", "bitcoin"], ["t", "nostr"], ["source", "https://blog.example/feed.xml"],
    ["imeta", f"url {PICTURE}", "m image/png"],
    ["imeta", "url https://cdn.example/removed.png", "m image/png"],
]


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


def tag_values(tags, name):
    return [tag[1] for tag in tags if tag[0] == name]


def test_the_details_are_read_from_a_drafts_tags():
    details = ArticleDetails.from_tags(IMPORTED_TAGS)
    assert (details.identifier, details.title, details.summary, details.image) == (
        "imported-post", "Imported", "What it is about", COVER)
    assert details.hashtags == ("bitcoin", "nostr")
    assert details.published_at == FIRST
    assert [tag[0] for tag in details.carried] == ["source", "imeta", "imeta"]


def test_a_draft_keeps_every_detail_and_only_pictures_still_in_it():
    tags = ArticleDetails.from_tags(IMPORTED_TAGS).draft_tags(f"Text ![x]({PICTURE})")
    assert tag_values(tags, "d") == ["imported-post"]
    assert tag_values(tags, "image") == [COVER]
    assert tag_values(tags, "published_at") == [str(FIRST)]
    assert tag_values(tags, "t") == ["bitcoin", "nostr"]
    assert tag_values(tags, "source") == ["https://blog.example/feed.xml"]
    assert tag_values(tags, "imeta") == [f"url {PICTURE}"]          # the removed one is gone


def test_kept_as_tags_the_details_come_back_whole():
    # A tab written down for a restart keeps them this way: every carried
    # tag too, whether or not the text still shows its picture.
    details = ArticleDetails.from_tags(IMPORTED_TAGS)
    assert ArticleDetails.from_tags(details.tags()) == details
    assert tag_values(details.tags(), "imeta") == [
        f"url {PICTURE}", "url https://cdn.example/removed.png"]


@pytest.mark.parametrize("tags", [None, [], [["d"]], [["published_at", "soon"]], [[1, 2]]])
def test_tags_without_details_give_empty_details(tags):
    details = ArticleDetails.from_tags(tags)
    assert details.published_at is None and details.identifier == ""


# -- saving a draft again (Ctrl+S) ------------------------------------------------------

def _window_with_draft(tags):
    """The parts of the window the draft builders use."""
    from main_window import MainWindow
    record = types.SimpleNamespace(inner_tags=tags)
    stand_in = types.SimpleNamespace(_draft_store=types.SimpleNamespace(get=lambda _d: record))
    stand_in._article_details_of = lambda ed: MainWindow._article_details_of(stand_in, ed)
    stand_in._author_of = lambda profile: MainWindow._author_of(stand_in, profile)
    return MainWindow, stand_in


def _bound_editor(identifier="imported-post", title="Imported, edited"):
    from main_window import DraftBinding
    from nostr.drafts import INNER_KIND_LONG_FORM
    return types.SimpleNamespace(_draft_binding=DraftBinding(
        identifier=identifier, inner_kind=INNER_KIND_LONG_FORM, title=title))


def test_saving_an_imported_draft_again_keeps_its_details():
    from nostr.ui.stash_kind_dialog import StashChoice, StashKind
    window_class, window = _window_with_draft(IMPORTED_TAGS)
    ed = _bound_editor()
    details = window._article_details_of(ed)
    assert details.title == "Imported, edited"                  # the tab's own title
    profile = types.SimpleNamespace(user_pubkey=PK)
    choice = StashChoice(kind=StashKind.ARTICLE, identifier="imported-post",
                         title=details.title, summary=details.summary)
    inner = window_class._build_inner_for_choice(window, profile, choice, "New text",
                                                 details=details)
    tags = inner["tags"]
    assert tag_values(tags, "published_at") == [str(FIRST)]
    assert tag_values(tags, "image") == [COVER]
    assert tag_values(tags, "t") == ["bitcoin", "nostr"]
    assert tag_values(tags, "source") == ["https://blog.example/feed.xml"]
    assert tag_values(tags, "title") == ["Imported, edited"]


def test_saved_under_another_identifier_it_has_no_first_publication_yet():
    from nostr.ui.stash_kind_dialog import StashChoice, StashKind
    window_class, window = _window_with_draft(IMPORTED_TAGS)
    details = window._article_details_of(_bound_editor())
    choice = StashChoice(kind=StashKind.ARTICLE, identifier="a-new-article", title="New")
    inner = window_class._build_inner_for_choice(
        window, types.SimpleNamespace(user_pubkey=PK), choice, "Text", details=details)
    assert tag_values(inner["tags"], "d") == ["a-new-article"]
    assert tag_values(inner["tags"], "published_at") == []
    assert tag_values(inner["tags"], "image") == [COVER]


# -- the publish dialog ------------------------------------------------------------------

def _dialog(tmp_path, details, **more):
    from nostr.avatar_store import AvatarStore
    from nostr.known_people import KnownPeople
    from nostr.profiles import Profile as StoredProfile
    from nostr.profiles import ProfileStore
    from nostr.search import Nip50SearchClient
    from nostr.ui.publish_article_dialog import PublishArticleDialog
    from tests.outbox_fakes import FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool
    from tests.outbox_fakes import HandPool, PK as OWNER

    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = StoredProfile(user_pubkey=OWNER, bunker_pubkey="b" * 64,
                            bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64)
    store.upsert(profile)
    people = KnownPeople(path=tmp_path / "people.json")
    return PublishArticleDialog(
        body_markdown=f"# Hello\n\nBody ![x]({PICTURE}).", active_profile=profile,
        store=store, relay_pool=FakePool(), relay_directory=FakeRelayDirectory(),
        session_pool=FakeSessionPool(FakeClient()), known_people=people,
        search_client=Nip50SearchClient(HandPool(), people), avatars=AvatarStore(),
        default_title=details.title, default_slug=details.identifier,
        first_published=details.published_at, details=details,
        published_at_lookup=lambda *_args: None, **more)


def test_the_publish_dialog_offers_and_publishes_the_drafts_details(tmp_path, monkeypatch):
    import nostr.ui.publish_article_dialog as module
    made = []

    class Captured:
        def __init__(self, **kwargs):
            made.append(kwargs["unsigned_event"])
            for name in ("status_changed", "signed", "completed", "failed"):
                setattr(self, name, types.SimpleNamespace(connect=lambda *_a: None))

        def start(self):
            pass

    monkeypatch.setattr(module, "PublishJob", Captured)
    details = ArticleDetails.from_tags(IMPORTED_TAGS)
    dialog = _dialog(tmp_path, details)
    assert dialog._summary_edit.text() == "What it is about"
    assert dialog._image_edit.text() == COVER
    assert dialog._hashtags.tags() == ("bitcoin", "nostr")
    dialog._title_edit.setText("A new title")
    assert dialog._slug_edit.text() == "imported-post"        # the article keeps its identifier
    dialog._on_publish()
    tags = made[0]["tags"]
    assert tag_values(tags, "published_at") == [str(FIRST)]
    assert tag_values(tags, "image") == [COVER]
    assert tag_values(tags, "t") == ["bitcoin", "nostr"]
    assert tag_values(tags, "source") == ["https://blog.example/feed.xml"]
    assert tag_values(tags, "imeta") == [f"url {PICTURE}"]


def test_tags_another_app_gave_an_article_survive_editing_it_here():
    # Review N7: Edit, then Publish, used to drop zap splits, a content
    # warning, alt text and labels; the new version replaced the old one.
    from nostr.publisher import CLIENT_NAME, build_article
    pk = "a" * 64
    theirs = [["d", "my-post"], ["title", "My Post"], ["published_at", "1700000000"],
              ["zap", pk, "wss://r.example", "1"], ["zap", "b" * 64, "", "2"],
              ["content-warning", "spoilers"], ["alt", "An article"], ["L", "ugc"],
              ["l", "review", "ugc"], ["client", "OtherApp"], ["p", "c" * 64],
              ["q", "d" * 64]]
    details = ArticleDetails.from_tags(theirs)
    # Zap shares are a detail of their own (Article Details shows them),
    # kept as written; the rest is carried.
    assert [list(tag) for tag in details.zaps] == [
        ["zap", pk, "wss://r.example", "1"], ["zap", "b" * 64, "", "2"]]
    assert [list(tag) for tag in details.carried] == [
        ["content-warning", "spoilers"], ["alt", "An article"], ["L", "ugc"],
        ["l", "review", "ugc"]]                           # q is made from the text
    event = build_article(content="The body.", pubkey_hex=pk, slug=details.identifier,
                          title=details.title, published_at=details.published_at,
                          extra_tags=details.published_tags("The body."))
    tags = event["tags"]
    for tag in theirs[3:9]:
        assert tag in tags
    assert not [tag for tag in tags if tag[0] == "q"]   # "The body." quotes nothing
    assert [tag for tag in tags if tag[0] == "client"] == [["client", CLIENT_NAME]]
    assert ["p", "c" * 64] not in tags                   # made again from the text


def test_an_edited_article_tags_each_quote_in_its_text_once():
    # Published carries the tags another app gave an article; the quote
    # tags are made from the text instead, so a quote kept in the text is
    # tagged once and a quote taken out of it is tagged no more.
    from nostr import bech32
    from nostr.publisher import build_article
    pk, kept, removed = "a" * 64, "d" * 64, "e" * 64
    theirs = [["d", "my-post"], ["title", "My Post"], ["q", kept], ["q", removed]]
    details = ArticleDetails.from_tags(theirs)
    body = "As said in nostr:" + bech32.encode_note(kept) + " before."
    event = build_article(content=body, pubkey_hex=pk, slug=details.identifier,
                          title=details.title, extra_tags=details.carried_tags(body))
    assert [tag[1] for tag in event["tags"] if tag[0] == "q"] == [kept]


# -- a detail the file keeps as written, in every window (round 8, M2) ----------

def kept():
    """Details whose title and hashtags the file keeps as written."""
    return ArticleDetails(title="Hello", summary="S", identifier="hello",
                          hashtags=("a",)).keeping({"title", "tags"})


def test_a_kept_detail_never_changes_in_the_model():
    """Whatever window hands the details back, one the file keeps as written
    stays as the file has it: the rule is the model's, not a widget's."""
    details = kept()
    changed = details.with_changes(title="A real title", summary="New", hashtags=("b",))
    assert (changed.title, changed.summary, changed.hashtags) == ("Hello", "New", ("a",))
    taken = details.with_details_of(ArticleDetails(title="Other", summary="Other",
                                                   identifier="x"))
    assert (taken.title, taken.summary, taken.identifier) == ("Hello", "Other", "x")
    assert taken.kept == details.kept and details.kept_fields == {"title", "hashtags"}


def test_the_publish_window_shows_a_kept_detail_read_only_saying_so(tmp_path, qt_app):
    from PySide6.QtWidgets import QLabel
    dialog = _dialog(tmp_path, kept())
    dialog.show()
    QApplication.processEvents()
    notes = [label for label in dialog.findChildren(QLabel)
             if label.text() == "Kept as written in the file."]
    assert dialog._title_edit.isReadOnly() and dialog._hashtags.isReadOnly()
    assert not dialog._summary_edit.isReadOnly() and len(notes) == 2
    dialog._set_busy(True)
    dialog._set_busy(False)
    assert dialog._title_edit.isReadOnly() and dialog._hashtags.isReadOnly()
    dialog._title_edit.setText("A real title")
    assert dialog.details().title == "Hello"
    dialog.close()


def test_save_as_draft_shows_a_kept_detail_read_only_saying_so(qt_app):
    from nostr.ui.stash_kind_dialog import StashKind, StashKindDialog
    dialog = StashKindDialog(default=StashKind.ARTICLE, suggested_title="Hello",
                             suggested_slug="hello", kept={"title"})
    dialog.show()
    QApplication.processEvents()
    notes = [label for label in dialog.findChildren(type(dialog._slug_problem))
             if label.text() == "Kept as written in the file." and label.isVisibleTo(dialog)]
    assert dialog._title_edit.isReadOnly() and not dialog._slug_edit.isReadOnly()
    assert len(notes) == 1
    dialog.close()


class _AnyMediaStore:
    """A media store the cover only asks whether there is one: its signals
    connect to nothing, its files are none."""
    files: dict = {}

    def __getattr__(self, name):
        if name.startswith("upload_"):
            return types.SimpleNamespace(connect=lambda *args: None)
        return lambda *args, **kwargs: None


def test_a_cover_being_worked_on_lets_nothing_be_picked(qt_app):
    """Round 10, M2: while the publish window worked, "Choose…" stayed
    enabled. Nothing is picked, typed or removed then, nor for a cover kept
    as written; Choose says where a picture goes."""
    from nostr.ui.cover_field import CoverImageField
    cover = CoverImageField(media_store=_AnyMediaStore(), stacked=True)
    cover.show()
    assert "upload" in cover.pick_button.toolTip()
    cover.setBusy(True)
    assert not cover.pick_button.isEnabled() and cover.url_edit.isReadOnly()
    assert not cover.remove_button.isEnabled() and not cover.preview.isEnabled()
    cover.setBusy(False)
    assert cover.pick_button.isEnabled() and not cover.url_edit.isReadOnly()
    assert cover.remove_button.isEnabled() and cover.preview.isEnabled()
    cover.setReadOnly(True)
    cover.setBusy(True)
    cover.setBusy(False)
    assert not cover.pick_button.isEnabled() and cover.url_edit.isReadOnly()
    assert not cover.preview.isEnabled()
    cover.close()


def test_the_publish_window_lets_no_cover_be_picked_while_it_works(tmp_path, qt_app):
    dialog = _dialog(tmp_path, ArticleDetails(title="T"), media_store=_AnyMediaStore())
    dialog.show()
    QApplication.processEvents()
    dialog._set_busy(True)
    assert not dialog._cover.pick_button.isEnabled()
    dialog._set_busy(False)
    assert dialog._cover.pick_button.isEnabled()
    dialog.close()


# -- the title an article that names none is offered ----------------------------------

NPUB = "npub1f2ktq2xcmh9wpmv0s6wqu6r9uvlvp7qr6sdqqk87x3dmnc9w0juswgtagx"


def test_the_default_title_is_the_first_heading():
    from nostr.article_details import default_title
    assert default_title("An opening line.\n\n## *The* heading\n\nMore.") == "The heading"
    assert default_title("```\n# not a heading\n```\n\n# Real one") == "Real one"


def test_without_a_heading_it_is_the_first_line_as_plain_text():
    from nostr.article_details import default_title
    assert default_title("*fafuui S s* and **more**\n\nBody.") == "fafuui S s and more"
    assert default_title("![hero](https://x/y.png)\n\nSee [the post](https://x/p).") \
        == "See the post."
    assert default_title("") == ""


def test_a_mention_in_the_title_is_its_name_never_its_key():
    from nostr import bech32
    from nostr.article_details import default_title
    pubkey = bech32.decode_npub(NPUB)
    names = {pubkey: "DrShift"}.get
    assert default_title(f"*fafuui S s*nostr:{NPUB}", names) == "fafuui S s @DrShift"
    unknown = default_title(f"*fafuui S s*nostr:{NPUB}")
    assert unknown == "fafuui S s @npub1f2kt…tagx" and len(unknown) < 30
    assert default_title(f"`nostr:{NPUB}` in code") == f"nostr:{NPUB} in code"


def test_the_window_offers_and_previews_the_same_default_title():
    """Publish as Article and the preview take an article's title from the
    one rule (article_details.default_title), not a line of their own."""
    import inspect
    from main_window import MainWindow
    for method in (MainWindow._open_publish_article, MainWindow._preview_content):
        source = inspect.getsource(method)
        assert "default_title(" in source and 'lstrip("# ")' not in source


def test_a_carried_picture_description_needs_the_address_itself_in_the_text():
    """NIP-92, as for the pictures described here: a description another
    app gave is carried only while its address is in the text, not when
    the text only holds a longer address that contains it."""
    details = ArticleDetails.from_tags(IMPORTED_TAGS)
    assert details.carried_tags(f"![x]({PICTURE})")[-1][1] == f"url {PICTURE}"
    assert not any(tag[0] == "imeta" for tag in
                   details.carried_tags(f"See https://proxy.example/?u={PICTURE} now."))


def test_a_title_reads_every_kind_of_reference_never_as_a_key():
    """Review low 2: a first line that quotes a note or an article gave a
    raw, cut key as the title; "(nostr:npub…)" gave "( @Name)"."""
    from nostr import bech32
    from nostr.article_details import default_title
    pubkey = bech32.decode_npub(NPUB)
    names = {pubkey: "DrShift"}.get
    nprofile = bech32.encode_nprofile(pubkey, ["wss://relay.example.com"])
    nevent = bech32.encode_nevent("e5" * 32, ["wss://relay.example.com"], pubkey)
    note = bech32.encode_note("e5" * 32)
    naddr = bech32.encode_naddr(identifier="my-post", author_pubkey_hex=pubkey, kind=30023,
                                relays=[])
    assert default_title(f"Thanks (nostr:{NPUB}) and nostr:{nprofile}!", names) \
        == "Thanks (@DrShift) and @DrShift!"
    assert default_title(f"nostr:{nevent}\n\nWhy it matters.", names) == "Quoted note"
    assert default_title(f"As nostr:{note} says", names) == "As Quoted note says"
    assert default_title(f"Read nostr:{naddr} first", names) == "Read Article first"
    for title in (default_title(f"x nostr:{e}") for e in (NPUB, nprofile, nevent, note, naddr)):
        assert "nostr:" not in title and max(len(word) for word in title.split()) < 20


def test_a_files_link_name_is_made_from_its_name_as_a_title_is(tmp_path):
    """Review low 4: a file-backed tab's link name was the file's name as
    it is ("Grüße an alle"), spaces and all. It is made the way a title's
    is now; an article this computer published under the file's own name
    keeps that name, so republishing it replaces it."""
    import types
    from main_window import MainWindow
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    window = types.SimpleNamespace(_first_publications=record)
    name_of = lambda path: MainWindow._link_name_for_file(window, path, PK)  # noqa: E731
    assert name_of("/notes/Grüße an alle.md") == "grusse-an-alle"
    assert name_of("") == ""
    record.remember(PK, "Old Post", 1_700_000_000)
    assert name_of("/notes/Old Post.md") == "Old Post"


def test_the_link_name_shown_before_one_is_typed_is_the_sheets():
    """Review round 3: for a file's article Article Details offered the
    link name made of the title, and Publish as Article the one made of
    the file's name. Both show the sheet's now."""
    import inspect
    from main_window import MainWindow
    from nostr.ui.article_details_panel import ArticleDetailsPanel
    panel = ArticleDetailsPanel()
    panel.show_article(ArticleDetails(title="Hello"), fixed_identifier=False,
                       made_identifier="grusse-an-alle")
    assert panel.identifier_edit.placeholderText() == "grusse-an-alle"
    panel.title_edit.setText("Another title")
    assert panel.identifier_edit.placeholderText() == "grusse-an-alle"
    panel.show_article(ArticleDetails(title="Hello"), fixed_identifier=False)
    assert panel.identifier_edit.placeholderText() == "hello"
    assert "made_identifier=" in inspect.getsource(MainWindow._fill_article_details_panel)


@pytest.mark.parametrize("tags, language", [
    ([["L", "ISO-639-1"], ["l", "de", "ISO-639-1"]], "de"),
    ([["l", "en-US", "ISO-639-1"]], "en"),
    ([["l", "pt_BR", "ISO-639-1"]], "pt"),
    ([["l", "de"]], ""),                                    # a bare label is some other label
    ([["l", "german", "ISO-639-1"]], ""),
    ([["l", "de", "other"]], ""),
    ([], ""),
])
def test_an_article_names_its_language_only_by_its_own_iso_label(tags, language):
    # NIP-32 as STANDUP reads it: no language is better than a wrong one.
    assert ArticleDetails.from_tags(tags).language == language
