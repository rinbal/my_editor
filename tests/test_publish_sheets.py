# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins Publish as Article and Publish as Note as sheets on the document's
window (nostr/ui/publish_article_dialog.py, publish_note_dialog.py and
publish_sheet.py).

What must hold:

  Both are sheets: window-modal, a sheet on macOS, opened with open()
  (never exec()); what changed in Publish as Article goes back to the
  document when it closes, published or not (its link name and first
  publication date once published). A note is composed for sharing and
  changes nothing in the document.

  Publish as Article has no second copy of the text: it publishes the
  document's text. Its labels are in sentence case and end in a colon,
  at the trailing edge of their column on a Mac. The card, named
  "Preview", shows the title, summary, cover and the author with the
  reading time, and follows the fields. Hashtags are tags and are
  published as t tags. The link name follows the title until it is
  typed, shows how it ends the article's address, sits under Show
  Details (Show Details / Hide Details), and an article that has one
  keeps it.

  People is everyone the event tags, by name, never by key: those the
  text mentions (read-only, "mentioned"), the authors of what it quotes
  ("quoted"), and the co-authors ("co-author"), added with the person
  picker and removed with x as in Article Details; the event's p tags
  are exactly the people shown.

  A failure is said in the status line in the theme's red; every
  control has a name; Tab goes top to bottom, left to right.

  In a note, a mention shows as "@Name", whether it was in the text,
  pasted, or chosen after typing "@"; Backspace takes it away whole;
  published and copied it is the person's nostr: address.
"""

import os
import re
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QObject, Qt, Signal  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QPlainTextEdit, QTextEdit, QWidget,
)

import theme  # noqa: E402
from nostr.ui.appearance import roles  # noqa: E402
from nostr import bech32  # noqa: E402
from nostr.article_details import AUTHOR_MARKER, ArticleDetails  # noqa: E402
from nostr.blossom.store import MediaFile  # noqa: E402
from nostr.known_people import KnownPeople, Person  # noqa: E402
from nostr.ui import publish_article_dialog as article_module  # noqa: E402
from nostr.ui import publish_note_dialog as note_module  # noqa: E402
from tests.accessibility import unnamed_controls  # noqa: E402
from tests.outbox_fakes import (  # noqa: E402
    PK, FakeClient, FakePool, FakeRelayDirectory, FakeSessionPool, HandPool,
)

BOB = bech32.decode_npub("npub1f2ktq2xcmh9wpmv0s6wqu6r9uvlvp7qr6sdqqk87x3dmnc9w0juswgtagx")
BOB_NPUB = bech32.encode_npub(BOB)
CAROL = "c3" * 32
STRANGER = "d4" * 32
NOTE_ID = "e5" * 32
RELAY = "wss://relay.example.com"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    return QApplication.instance() or QApplication([])


class Captured:
    """Stands in for PublishJob: keeps the event it was given."""

    made = []

    def __init__(self, **kwargs):
        Captured.made.append(kwargs["unsigned_event"])
        for name in ("status_changed", "signed", "completed", "failed"):
            setattr(self, name, types.SimpleNamespace(connect=lambda *_a: None))

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture
def captured(monkeypatch):
    Captured.made = []
    monkeypatch.setattr(article_module, "PublishJob", Captured)
    monkeypatch.setattr(note_module, "PublishJob", Captured)
    return Captured.made


def common(tmp_path, **more):
    from nostr.avatar_store import AvatarStore
    from nostr.profiles import Profile, ProfileStore
    from nostr.search import Nip50SearchClient
    store = ProfileStore(path=tmp_path / "profiles.json")
    profile = Profile(user_pubkey=PK, bunker_pubkey="b" * 64,
                      bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64,
                      display_name="DrShift")
    store.upsert(profile)
    people = KnownPeople(path=tmp_path / "people.json")
    people.upsert(Person(pubkey=BOB, display_name="Bob Writer"))
    people.upsert(Person(pubkey=CAROL, display_name="Carol"))
    args = dict(active_profile=profile, store=store, relay_pool=FakePool(),
                relay_directory=FakeRelayDirectory(), session_pool=FakeSessionPool(FakeClient()),
                known_people=people, search_client=Nip50SearchClient(HandPool(), people),
                avatars=AvatarStore(), is_dark=False)
    args.update(more)
    return args


def article(tmp_path, body=None, **more):
    more.setdefault("published_at_lookup", lambda author, slug, done: None)
    body = body if body is not None else f"*Fafuui S s*nostr:{BOB_NPUB}\n\nThe article."
    return article_module.PublishArticleDialog(body_markdown=body, **common(tmp_path, **more))


def note(tmp_path, content=None, **more):
    content = content if content is not None else f"Thanks nostr:{BOB_NPUB} for the review!"
    return note_module.PublishNoteDialog(content=content, **common(tmp_path, **more))


def p_tags(event):
    return [tag for tag in event["tags"] if tag[0] == "p"]


# -- both: sheets on the document's window ------------------------------------------------

@pytest.mark.parametrize("make", [article, note])
def test_both_are_sheets_on_their_window(tmp_path, make):
    sheet = make(tmp_path)
    assert sheet.windowModality() == Qt.WindowModality.WindowModal
    assert bool(sheet.windowFlags() & Qt.WindowType.Sheet & ~Qt.WindowType.Window) == (
        sys.platform == "darwin")
    sheet.close()


def test_the_window_opens_them_as_sheets_and_takes_back_what_changed():
    import inspect
    from main_window import MainWindow
    for handler in (MainWindow._open_publish_article, MainWindow._open_publish_note):
        source = inspect.getsource(handler)
        assert "dialog.open()" in source and ".exec()" not in source
    article_source = inspect.getsource(MainWindow._open_publish_article)
    assert "dialog.finished.connect" in article_source
    # A note is composed for sharing: nothing goes back to the document.
    assert "finished" not in inspect.getsource(MainWindow._open_publish_note)


def test_what_changed_goes_back_when_the_sheet_closes_published_or_not(tmp_path):
    from main_window import MainWindow
    from editor import HtmlEditor
    sheet = article(tmp_path)
    ed = HtmlEditor()
    set_ = []
    window = types.SimpleNamespace(_set_article_details=lambda e, d: set_.append((e, d)))
    MainWindow._article_sheet_closed(window, sheet, ed)
    assert set_ == []                                         # nothing changed
    sheet._summary_edit.setText("One sentence.")
    sheet.reject()
    MainWindow._article_sheet_closed(window, sheet, ed)
    assert set_[0][0] is ed and set_[0][1].summary == "One sentence."


# -- Publish as Article ---------------------------------------------------------------------

def test_it_publishes_the_documents_text_and_has_no_copy_of_it(tmp_path, captured):
    sheet = article(tmp_path, body="# Hello\n\nBody.", default_title="Hello")
    assert sheet.findChildren(QPlainTextEdit) == [] and sheet.findChildren(QTextEdit) == []
    sheet._on_publish()
    # (no date known: asked of the relays first)
    sheet._on_published_at("hello", PK, article_module.FirstPublication(article_module.NEVER))
    assert captured[0]["content"] == "# Hello\n\nBody."


def test_labels_are_sentence_case_with_a_colon_on_the_platforms_side(tmp_path):
    sheet = article(tmp_path)
    form = sheet._form
    words = [label.text() for label in sheet._labels if label.text()]
    assert words == ["Title:", "Summary:", "Hashtags:", "Cover image:", "People:",
                     "Link name:", "Cover image address:"]
    assert all(word == word[0] + word[1:].lower() for word in words)
    trailing = Qt.AlignmentFlag.AlignRight if sys.platform == "darwin" \
        else Qt.AlignmentFlag.AlignLeft
    assert form.labelAlignment() & trailing
    assert all(label.alignment() & trailing for label in sheet._labels)
    assert sheet._title_edit.font().pointSizeF() == QApplication.font().pointSizeF()


def test_the_card_shows_the_article_as_readers_see_it_and_follows_the_fields(tmp_path):
    sheet = article(tmp_path, default_title="Fafuui S s @Bob Writer")
    card = sheet._card
    assert card.accessibleName() == "Preview"
    assert card.title.text() == "Fafuui S s @Bob Writer"
    assert card.byline.text() == "DrShift · 1 min read"
    sheet._summary_edit.setText("Why a calm editor matters.")
    sheet._title_edit.setText("A new title")
    assert card.title.text() == "A new title" and card.summary.text() == \
        "Why a calm editor matters."
    assert "A new title" in card.accessibleDescription()
    picture = QPixmap(64, 36)
    picture.fill(QColor("#336699"))
    sheet._cover._picture = picture
    sheet._cover.picture_changed.emit()
    assert not card.cover.pixmap().isNull()
    sheet._title_edit.setText("")
    assert card.title.text() == "Untitled"


def test_hashtags_are_tags_published_as_t_tags(tmp_path, captured):
    sheet = article(tmp_path, default_title="Hello",
                    details=ArticleDetails(hashtags=("bitcoin",)))
    QTest.keyClicks(sheet._hashtags.entry, "#Essay, nostr")
    QTest.keyClick(sheet._hashtags.entry, Qt.Key.Key_Return)
    assert sheet.details().hashtags == ("bitcoin", "Essay", "nostr")
    sheet._publish("hello", sheet._body, 1_800_000_000)
    assert [tag[1] for tag in captured[0]["tags"] if tag[0] == "t"] == [
        "bitcoin", "essay", "nostr"]


def test_the_link_name_follows_the_title_until_typed_and_says_how_it_ends(tmp_path):
    sheet = article(tmp_path, default_title="Fafuui S s @Bob Writer")
    assert sheet._details_toggle.text() == "Show Details"
    assert not sheet._slug_edit.isVisibleTo(sheet)
    sheet._details_toggle.click()
    assert sheet._details_toggle.text() == "Hide Details"
    assert sheet._slug_edit.isVisibleTo(sheet) and sheet._image_edit.isVisibleTo(sheet)
    assert sheet._slug_edit.text() == "fafuui-s-s-bob-writer"
    assert sheet._slug_hint.text() == "…/fafuui-s-s-bob-writer"
    sheet._title_edit.setText(f"Mine nostr:{BOB_NPUB}")
    assert sheet._slug_edit.text() == "mine"                    # never the key
    QTest.keyClicks(sheet._slug_edit, "-too")
    sheet._title_edit.setText("Another title")
    assert sheet._slug_edit.text() == "mine-too"                # typed: it stays


def test_an_article_that_has_a_link_name_keeps_it(tmp_path):
    sheet = article(tmp_path, default_title="T", default_slug="imported-post",
                    details=ArticleDetails(identifier="imported-post", title="T"))
    sheet._title_edit.setText("A new title")
    assert sheet._slug_edit.text() == "imported-post"


def test_people_are_everyone_tagged_by_name_never_by_key(tmp_path, captured):
    quoted = bech32.encode_nevent(NOTE_ID, [RELAY], STRANGER)
    body = (f"*Fafuui S s*nostr:{BOB_NPUB}\n\nAs this note says:\n\nnostr:{quoted}\n")
    sheet = article(tmp_path, body=body, default_title="T",
                    details=ArticleDetails(coauthors=((CAROL, ""),)))
    names = [name for _pubkey, name in sheet._people.people()]
    stranger = "@" not in names[1] and names[1].startswith("npub1") and "…" in names[1]
    assert names[0] == "Bob Writer, mentioned" and stranger and names[1].endswith(", quoted")
    assert names[2] == "Carol, co-author"
    assert not any(BOB_NPUB in label.text() or BOB in label.text()
                   for label in sheet._people.findChildren(QLabel))
    sheet._publish("t", sheet._body, 1_800_000_000)
    tagged = [tag[1] for tag in p_tags(captured[0]) if tag[1] != PK]
    assert tagged == [pubkey for pubkey, _name in sheet._people.people()]


def test_co_authors_are_added_and_removed_as_in_article_details(tmp_path, captured):
    sheet = article(tmp_path, default_title="T")
    sheet._add_coauthor.click()
    picker = sheet._coauthor_picker
    assert picker is not None and picker.isVisible()
    picker.picked.emit(Person(pubkey=CAROL, display_name="Carol", relay_hint=RELAY))
    picker.picked.emit(Person(pubkey=CAROL))                   # twice: nothing
    picker.picked.emit(Person(pubkey=PK))                      # the author: nothing
    assert sheet.details().coauthors == ((CAROL, RELAY),) and sheet.details_changed()
    assert [name for _p, name in sheet._people.people()][-1] == "Carol, co-author"
    sheet._publish("t", sheet._body, 1_800_000_000)
    assert ["p", CAROL, RELAY, AUTHOR_MARKER] in p_tags(captured[0])
    sheet._on_failed("relay down")                            # to try again
    remove = [b for b in sheet.findChildren(QWidget) if b.accessibleName() == "Remove Carol"]
    remove[0].click()
    assert sheet.details().coauthors == ()


def test_a_failure_is_said_in_red_and_publish_can_be_tried_again(tmp_path, captured):
    sheet = article(tmp_path, default_title="T")
    sheet._publish("t", sheet._body, 1_800_000_000)
    # While it goes out, nothing can change, and nothing moves.
    assert not sheet._publish_btn.isEnabled() and not sheet._hashtags.isEnabled()
    assert not sheet._hashtags.entry.isHidden() and not sheet._account.isEnabled()
    sheet._on_completed([("wss://a.example", False, "blocked")])
    assert sheet._hashtags.isEnabled() and sheet._account.isEnabled()
    assert not sheet._status.isHidden() and "no relay took it" in sheet._status.text()
    assert roles(False).error in sheet._status.styleSheet()
    assert sheet._publish_btn.isEnabled()


def test_every_control_is_named_and_tab_goes_top_to_bottom(tmp_path):
    sheet = article(tmp_path, default_title="T",
                    details=ArticleDetails(hashtags=("bitcoin",), coauthors=((CAROL, ""),)),
                    media_store=_Store())
    sheet._details_toggle.setChecked(True)
    sheet.show()
    QApplication.processEvents()
    assert unnamed_controls(sheet) == []
    chain, widget = [], sheet._title_edit
    for _step in range(16):
        chain.append(widget.accessibleName() or widget.text())
        widget = widget.nextInFocusChain()
        while not (widget.focusPolicy() & Qt.FocusPolicy.TabFocus
                   and widget.isVisibleTo(sheet) and widget.isEnabled()):
            widget = widget.nextInFocusChain()
    left_to_right = sorted((sheet._cancel_btn, sheet._publish_btn), key=lambda b: b.x())
    assert chain[:13] == [
        "Title", "Summary", "Remove bitcoin", "Hashtags", "Choose Cover Image",
        "Remove Carol", "Add Co-Author…", "Hide Details", "Link name",
        "Cover image address", "Publishing as DrShift"] + [b.text() for b in left_to_right]
    sheet.close()


class _Store(QObject):
    """A media store that holds nothing and uploads nothing."""

    upload_finished = Signal(str, object)
    upload_failed = Signal(str, str)
    upload_progress = Signal(str, int, int)
    files: dict = {}

    def upload_bytes(self, body, *, name, mime_type="application/octet-stream"):
        self.uploaded = name


# -- Publish as Note ----------------------------------------------------------------------

def test_a_mention_in_a_note_is_a_name_and_goes_out_as_its_address(tmp_path, captured):
    sheet = note(tmp_path)
    assert sheet._text_edit.toPlainText() == "Thanks @Bob Writer for the review!"
    assert sheet._text_edit.note() == f"Thanks nostr:{BOB_NPUB} for the review!"
    assert [name for _p, name in sheet._people.people()] == ["Bob Writer, mentioned"]
    sheet._on_publish()
    assert captured[0]["content"] == f"Thanks nostr:{BOB_NPUB} for the review!"
    assert [tag[1] for tag in p_tags(captured[0])] == [BOB]


def test_a_pasted_address_shows_the_name_and_copies_as_the_address(tmp_path):
    sheet = note(tmp_path, content="Hello ")
    assert sheet._people_line.isHidden()                    # nobody tagged: no row
    cursor = sheet._text_edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    sheet._text_edit.setTextCursor(cursor)
    data = QMimeData()
    data.setText(f"nostr:{BOB_NPUB} and nostr:{bech32.encode_npub(STRANGER)}")
    sheet._text_edit.insertFromMimeData(data)
    shown = sheet._text_edit.toPlainText()
    assert shown.startswith("Hello @Bob Writer and @npub1") and "nostr:" not in shown
    assert not sheet._people_line.isHidden()
    sheet._text_edit.selectAll()
    assert sheet._text_edit.createMimeDataFromSelection().text() == sheet._text_edit.note()


def test_typing_at_asks_whom_to_mention_and_backspace_takes_it_whole(tmp_path):
    sheet = note(tmp_path, content="Hi")
    sheet.show()
    edit = sheet._text_edit
    edit.setFocus()
    cursor = edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    QTest.keyClicks(edit, " @")
    assert sheet._picker is not None and sheet._picker.isVisible()
    sheet._picker.picked.emit(Person(pubkey=CAROL, display_name="Carol"))
    assert edit.toPlainText() == "Hi @Carol "
    assert edit.note() == f"Hi nostr:{bech32.encode_npub(CAROL)}"
    QTest.keyClick(edit, Qt.Key.Key_Backspace)                # the space
    QTest.keyClick(edit, Qt.Key.Key_Backspace)                # the whole mention
    assert edit.toPlainText() == "Hi "
    QTest.keyClicks(edit, "me@example.com")                   # an email address: no picker
    sheet._picker.hide()
    assert edit.toPlainText() == "Hi me@example.com"
    sheet.close()


def test_the_note_sheet_names_every_control(tmp_path):
    sheet = note(tmp_path)
    assert unnamed_controls(sheet) == []
    sheet.close()


# -- one word for an article's identifier ----------------------------------------------------

def test_an_articles_identifier_is_its_link_name_wherever_it_is_named():
    """Publish as Article, Article Details, Save as Nostr Draft and the
    drafts' messages call the article's identifier its link name: never
    "slug", "article ID" or "d-tag", not even in an example."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "i18n_script", os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts", "i18n.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    messages, _problems = script.extract()
    named = [key[1] for key in messages
             if re.search(r"slug|article ID|d-tag", key[1], re.IGNORECASE)]
    assert named == []
    from nostr.ui.secret_key_check import SLUG_HOLDS_A_KEY
    assert "link name" in SLUG_HOLDS_A_KEY


def test_the_sheets_never_wrap_the_items_of_a_layout_qt_owns():
    """Asking a QDialogButtonBox's or a QFormLayout's layout for its items
    (itemAt) gives Python wrappers of items Qt deletes on its own: one sheet
    in a few hundred then crashed the app at a later allocation. The
    sheets keep their own lists, and ask the buttons where they are."""
    import inspect
    from nostr.ui import publish_sheet
    for module in (publish_sheet, article_module, note_module):
        assert ".itemAt(" not in inspect.getsource(module), module.__name__


def test_publish_waits_for_a_dropped_cover_and_a_failure_is_said_under_it(
        tmp_path, captured, monkeypatch):
    """Review M2: an article published while its dropped cover was still
    uploading went out without it."""
    from tests.test_cover_field import picture_file
    from nostr.ui import cover_field
    from tests.test_cover_field import FakeLoader
    monkeypatch.setattr(cover_field, "ThumbnailLoader", FakeLoader)
    store = _Store()
    sheet = article(tmp_path, default_title="T", media_store=store)
    sheet._cover.drop(str(picture_file(tmp_path)))
    assert not sheet._publish_btn.isEnabled()
    sheet._on_publish()
    assert captured == []
    url = f"https://blossom.example/{'ef' * 32}.png"
    store.upload_finished.emit(store.uploaded, MediaFile(hash="ef" * 32, url=url,
                                                         mime_type="image/png"))
    assert sheet._publish_btn.isEnabled()
    sheet._publish("t", sheet._body, 1_800_000_000)
    assert ["image", url] in captured[0]["tags"]
    sheet._on_failed("relay down")
    sheet._cover.drop(str(picture_file(tmp_path, "b.png")))
    store.upload_failed.emit(store.uploaded, "the server said no")
    assert sheet._publish_btn.isEnabled()
    assert not sheet._cover_problem.isHidden()
    assert "the server said no" in sheet._cover_problem.text()


@pytest.mark.parametrize("make", [article, note])
def test_a_sheet_follows_the_appearance_while_it_is_open(tmp_path, make):
    """Review M3: switched to Dark while a sheet was open, its labels came
    out dark on dark. A sheet takes the new appearance as the window
    does, everything in it."""
    from nostr.ui.publish_sheet import sheet_stylesheet
    from constants import DARK_MENU_BG
    sheet = make(tmp_path)                                   # opened light
    sheet._set_status("It went wrong", error=True)
    sheet.apply_theme(True)
    assert sheet.styleSheet() == sheet_stylesheet(True)
    assert roles(True).error in sheet._status.styleSheet()
    assert sheet._account._is_dark
    if make is article:
        assert DARK_MENU_BG in sheet._card.styleSheet()
        assert sheet._hashtags._is_dark and sheet._cover._is_dark
    else:
        assert sheet._text_edit._look._is_dark
    sheet.close()


def test_the_window_gives_an_open_sheet_its_new_appearance():
    import inspect
    from main_window import MainWindow
    assert "_publish_sheet" in inspect.getsource(MainWindow._set_theme)
    for handler in (MainWindow._open_publish_article, MainWindow._open_publish_note):
        assert "self._publish_sheet = dialog" in inspect.getsource(handler)


@pytest.mark.parametrize("make", [article, note])
def test_tab_reaches_the_buttons_left_to_right_on_the_first_open(tmp_path, make):
    """Review low 1: the order was set before the buttons were placed, so
    on the first open Tab went from the account to Publish, then Cancel,
    on a Mac where Cancel is at the left."""
    from PySide6.QtWidgets import QDialogButtonBox, QStyle

    def after_account():
        after, chain = sheet._account.button.nextInFocusChain(), []
        while len(chain) < 2:
            if after in (sheet._cancel_btn, sheet._publish_btn):
                chain.append(after)
            after = after.nextInFocusChain()
        return chain

    sheet = make(tmp_path, default_title="T") if make is article else make(tmp_path)
    box = sheet._publish_btn.parentWidget()
    layout = box.style().styleHint(QStyle.StyleHint.SH_DialogButtonLayout, None, box)
    cancel_first = layout in (QDialogButtonBox.ButtonLayout.MacLayout.value,
                              QDialogButtonBox.ButtonLayout.GnomeLayout.value)
    expected = ([sheet._cancel_btn, sheet._publish_btn] if cancel_first
                else [sheet._publish_btn, sheet._cancel_btn])
    assert after_account() == expected                      # before it is ever shown
    sheet.show()
    QApplication.processEvents()
    assert after_account() == expected == sorted(expected, key=lambda b: b.x())
    sheet.close()


def test_the_bottom_row_orders_tab_by_the_platforms_button_layout_not_by_where_they_are(
        tmp_path):
    """The order is the platform's (Cancel at the left on a Mac and in
    GNOME), known before any button is placed: on Cocoa the buttons are
    placed after the row is first laid out."""
    from PySide6.QtWidgets import QDialogButtonBox, QStyle
    from nostr.ui.publish_sheet import AccountRow, BottomRow
    args = common(tmp_path)
    account = AccountRow(args["active_profile"], args["store"], args["avatars"], is_dark=False)
    box = QDialogButtonBox()
    cancel = box.addButton(QDialogButtonBox.StandardButton.Cancel)
    publish = box.addButton("Publish", QDialogButtonBox.ButtonRole.AcceptRole)
    row = BottomRow(account, box)
    layout = box.style().styleHint(QStyle.StyleHint.SH_DialogButtonLayout, None, box)
    cancel_first = layout in (QDialogButtonBox.ButtonLayout.MacLayout.value,
                              QDialogButtonBox.ButtonLayout.GnomeLayout.value)
    after, chain = account.button.nextInFocusChain(), []
    while len(chain) < 2:
        if after in (cancel, publish):
            chain.append(after)
        after = after.nextInFocusChain()
    assert chain == ([cancel, publish] if cancel_first else [publish, cancel]) and row


def test_a_link_name_this_computer_published_under_stays_as_the_title_changes(tmp_path):
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    record.remember(PK, "Old Post", 1_700_000_000)
    sheet = article(tmp_path, default_title="Old Post", default_slug="Old Post",
                    first_publications=record)
    sheet._title_edit.setText("A better title")
    assert sheet._slug_edit.text() == "Old Post"
    fresh = article(tmp_path, default_title="New", default_slug="new",
                    first_publications=record)
    fresh._title_edit.setText("Newer")
    assert fresh._slug_edit.text() == "newer"                # never published: it follows


def test_the_card_asks_for_the_height_it_has_at_its_own_width(tmp_path):
    """Review low 7: under the fields (a 640x480 window) the card was
    measured as if as wide as the sheet, its words on fewer lines, and
    the fields above it lost the difference: the cover's well was cut."""
    sheet = article(tmp_path, default_title="A title long enough to take two lines or three "
                                             "in the card at its width")
    sheet._summary_edit.setText("A summary long enough to wrap onto several lines in the "
                                "preview card, as summaries do")
    card = sheet._card
    sheet.resize(560, 500)
    sheet.show()
    QApplication.processEvents()
    assert sheet._columns.direction() != sheet._columns.Direction.LeftToRight
    assert card.height() == card.layout().totalHeightForWidth(card.WIDTH)
    assert sheet._cover.height() >= sheet._cover.minimumSizeHint().height()
    sheet.close()


def test_typing_in_a_note_keeps_its_people_as_they_are(tmp_path):
    """Review low 8: every key typed made every person's chip again."""
    from nostr.ui.publish_sheet import _Person
    sheet = note(tmp_path)
    before = sheet._people.findChildren(_Person)
    QTest.keyClicks(sheet._text_edit, " and more words")
    QApplication.processEvents()
    assert sheet._people.findChildren(_Person) == before and len(before) == 1
    sheet._text_edit.setPlainText(f"Now nostr:{bech32.encode_npub(CAROL)} too")
    assert [name for _p, name in sheet._people.people()] == ["Carol, mentioned"]


def test_a_link_name_typed_by_hand_is_written_in_one_form(tmp_path, captured):
    """Review round 2, M2: a link name typed or pasted by hand was used as
    it came; "école" typed with a combining accent (as macOS file names
    hold it) made another d tag than the same word composed."""
    import unicodedata
    from nostr.publisher import link_name
    decomposed = unicodedata.normalize("NFD", "école-été")
    assert link_name(f"  {decomposed} ") == "école-été"
    sheet = article(tmp_path, default_title="T")
    sheet._slug_edit.setText(decomposed)
    sheet._on_slug_edited(decomposed)
    assert sheet._slug() == "école-été"
    sheet._publish(sheet._slug(), sheet._body, 1_800_000_000)
    assert ["d", "école-été"] in captured[0]["tags"]
    sheet._on_completed([("wss://nos.lol", True, "")])
    assert sheet.details().identifier == "école-été"
    from nostr.ui.article_details_panel import ArticleDetailsPanel
    from nostr.ui.stash_kind_dialog import StashKind, StashKindDialog
    panel = ArticleDetailsPanel()
    panel.show_article(ArticleDetails(title="T"), fixed_identifier=False)
    panel.identifier_edit.setText(decomposed)
    assert panel.details().identifier == "école-été"
    stash = StashKindDialog(default=StashKind.ARTICLE, suggested_slug="x")
    stash._slug_edit.setText(decomposed)
    stash._on_slug_edited(decomposed)
    stash._on_accept()
    assert stash.choice.identifier == "école-été"


FIRST = 1_600_000_000


def test_a_link_name_the_article_has_is_kept_letter_for_letter(tmp_path, captured):
    """Review round 3, M1: a Mac writes file names decomposed ("u" and an
    accent for "ü"), and 3.3 published a file's article under its name as
    it was. Its link name was written in the composed form wherever it
    was shown (the sheet, Article Details, the stash dialog) and
    republished so: relays compare it letter for letter, so that was a
    second article. A link name the article has already stays exactly as
    it is; only one typed by hand is written in one form."""
    import unicodedata
    from nostr.ui.article_details_panel import ArticleDetailsPanel
    from nostr.ui.stash_kind_dialog import StashKind, StashKindDialog
    kept = unicodedata.normalize("NFD", "Grüße an alle")
    assert kept != unicodedata.normalize("NFC", kept)
    asked = []
    sheet = article(tmp_path, default_title="T", default_slug=kept,
                    details=ArticleDetails(identifier=kept, title="T"),
                    published_at_lookup=lambda author, slug, done: asked.append(slug))
    assert sheet._slug() == kept and sheet.details().identifier == kept
    sheet._title_edit.setText("Another title")
    sheet._on_publish()
    assert asked == [kept]                               # asked as it is
    sheet._publish(sheet._slug(), sheet._body, FIRST)
    assert ["d", kept] in captured[0]["tags"]
    panel = ArticleDetailsPanel()
    panel.show_article(ArticleDetails(identifier=kept, title="T"), fixed_identifier=True)
    assert panel.details().identifier == kept
    panel.title_edit.setText("Another title")            # any field edited
    assert panel.details().identifier == kept
    panel.identifier_edit.setText(kept + "-2")           # typed: one form
    assert panel.details().identifier == unicodedata.normalize("NFC", kept + "-2")
    stash = StashKindDialog(default=StashKind.ARTICLE, suggested_slug=kept)
    stash._on_accept()
    assert stash.choice.identifier == kept


# "Grüße an alle" as a Mac gives a file's name ("u" and an accent), composed, and made.
RAW = "Gru\u0308\u00dfe an alle"
COMPOSED = "Gr\u00fc\u00dfe an alle"
MADE = "grusse-an-alle"


def file_article(tmp_path, asked, **more):
    """A file's article with no link name of its own, as the window opens
    it: the made link name, and the file's name as the file system gives it."""
    from nostr.first_publications import FirstPublications
    more.setdefault("first_publications", FirstPublications(tmp_path / "first.json"))
    return article(tmp_path, default_title="Hello", default_slug=MADE, file_name=RAW,
                   published_at_lookup=lambda author, slug, done, also=(): asked.append(
                       (author, slug, tuple(also), done)), **more)


@pytest.mark.parametrize("state", ["found", "never", "unknown", "unknown-new"])
def test_a_files_own_name_is_asked_about_at_publish_and_not_before(
        tmp_path, captured, monkeypatch, state):
    """Review round 3, M2 and lows 1 to 3: the file's own name was asked of
    the relays when the sheet opened, before the person chose to publish
    (and on Cancel too); with no answer it became the link name for good,
    and a late answer overwrote a link name typed or a title edited
    meanwhile. Nothing is asked before Publish now. At Publish the file's
    own names (as the file system gives it, and composed) are asked about
    with the made link name, in one question: found under one, the
    article goes out under exactly that name, and this computer records
    it; none found, under the made name; when nobody can tell, the person
    decides, told why."""
    from nostr.publisher import FOUND, NEVER, UNKNOWN, FirstPublication
    answers = {"found": FirstPublication(FOUND, FIRST, under=RAW),
               "never": FirstPublication(NEVER), "unknown": FirstPublication(UNKNOWN),
               "unknown-new": FirstPublication(UNKNOWN)}
    alerts = []
    monkeypatch.setattr(article_module, "ask", lambda *a, **k: alerts.append(k) or (
        "new" if state == "unknown-new" else "cancel"))
    asked = []
    sheet = file_article(tmp_path, asked)
    assert asked == [] and sheet._status.isHidden() and sheet._publish_btn.isEnabled()
    assert sheet._slug_edit.text() == MADE
    sheet._on_publish()
    [(author, slug, also, done)] = asked
    assert (author, slug, also) == (PK, MADE, (RAW, COMPOSED))
    done(answers[state])
    tags = captured[0]["tags"] if captured else []
    if state == "found":
        assert ["d", RAW] in tags and ["published_at", str(FIRST)] in tags
        # Recorded once it went out, not when it was found (review round 4).
        assert sheet._first_publications.get(PK, RAW) is None
        sheet._on_completed([("wss://nos.lol", True, "")])
        assert sheet._first_publications.get(PK, RAW) == FIRST
    elif state == "never":
        assert ["d", MADE] in tags
    else:
        import unicodedata
        assert COMPOSED in unicodedata.normalize("NFC", alerts[0]["message"])
        assert (["d", MADE] in tags) if state == "unknown-new" else captured == []


def test_when_nobody_can_tell_the_files_own_name_can_be_chosen_as_it_is(
        tmp_path, captured, monkeypatch):
    """Review round 4, L1: with no clear answer the person could wait or
    make another article, never keep the one under the file's own name:
    typing it gives the composed form, another name again for a Mac's
    file. The alert offers both names, each as it would go out; Try Again
    stays the default and Cancel backs out."""
    from alerts import CANCEL, DEFAULT, NORMAL
    from nostr.publisher import UNKNOWN, FirstPublication
    alerts = []
    monkeypatch.setattr(article_module, "ask", lambda *a, **k: alerts.append(k) or "file")
    asked = []
    sheet = file_article(tmp_path, asked)
    before = int(__import__("time").time())
    sheet._on_publish()
    asked[-1][3](FirstPublication(UNKNOWN))
    buttons = {button.label: button.role for button in alerts[0]["buttons"]}
    assert buttons == {f"Publish as “{RAW}”": NORMAL, f"Publish as “{MADE}”": NORMAL,
                       "Cancel": CANCEL, "Try Again": DEFAULT}
    tags = captured[0]["tags"]
    assert ["d", RAW] in tags                                  # exactly as it is
    assert int(next(t[1] for t in tags if t[0] == "published_at")) >= before
    assert sheet._slug_edit.text() == RAW and sheet._slug() == RAW


@pytest.mark.parametrize("name, made", [(RAW, MADE), ("My Post", "my-post")])
@pytest.mark.parametrize("state", ["found", "never"])
def test_a_files_own_name_is_asked_about_also_with_a_date_in_its_front_matter(
        tmp_path, captured, name, made, state):
    """Review round 4, B1: a Markdown file with a date in its front matter
    and no slug (as Jekyll, Hugo and Obsidian write them) went out under
    the made link name without a question: the known date kept the link
    name from following the title, and only then were the file's names
    asked about. 3.3 read no front matter and published the file under
    its own name, so that was a second article. The file's own names are
    asked about whenever the link name is the one made of the file's name;
    none found, the made name goes out with the front matter's date."""
    import front_matter
    from nostr.article_details import from_front_matter
    from nostr.first_publications import FirstPublications
    from nostr.publisher import FOUND, NEVER, FirstPublication, file_link_names
    front, _body = front_matter.split("---\ntitle: Hello\ndate: 2024-05-01\n---\nBody\n")
    details = from_front_matter(front)
    assert details.identifier == "" and details.published_at
    asked = []
    sheet = article(tmp_path, default_title="Hello", default_slug=made, file_name=name,
                    first_published=details.published_at, details=details,
                    first_publications=FirstPublications(tmp_path / "first.json"),
                    published_at_lookup=lambda a, s, done, also=(): asked.append(
                        (s, tuple(also), done)))
    sheet._on_publish()
    [(slug, also, done)] = asked
    assert (slug, also) == (made, tuple(file_link_names(name)))
    done(FirstPublication(FOUND, FIRST, under=name) if state == "found"
         else FirstPublication(NEVER))
    tags = captured[0]["tags"]
    if state == "found":
        assert ["d", name] in tags and ["published_at", str(FIRST)] in tags
    else:
        assert ["d", made] in tags and ["published_at", str(details.published_at)] in tags


def test_a_name_this_computer_published_under_is_not_asked_about_again(tmp_path, captured):
    """The made link name this computer published the file under is the
    article: its own names are not asked about."""
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    record.remember(PK, MADE, FIRST)
    asked = []
    sheet = file_article(tmp_path, asked, first_publications=record)
    sheet._on_publish()
    assert asked == [] and ["d", MADE] in captured[0]["tags"]


def test_found_under_the_files_name_but_not_published_records_nothing(
        tmp_path, captured, monkeypatch):
    """Review round 4, L3: this computer's record was written when the
    article was found under the file's name, before anything went out
    (the secret key check could still stop it). It is the record of what
    this computer published: written after a relay took it."""
    from nostr.publisher import FOUND, FirstPublication
    monkeypatch.setattr(article_module, "publishable", lambda *a, **k: None)
    asked = []
    sheet = file_article(tmp_path, asked)
    sheet._on_publish()
    asked[-1][3](FirstPublication(FOUND, FIRST, under=RAW))
    assert captured == [] and sheet._first_publications.get(PK, RAW) is None


def test_a_link_name_typed_or_another_account_is_what_publish_asks_about(tmp_path, captured):
    """Review round 3, lows 1 and 2: decided at Publish, a typed link name
    is the one asked about (no file name with it), and the account in use
    then is the one asked for."""
    from nostr.profiles import Profile
    asked = []
    sheet = file_article(tmp_path, asked)
    other = Profile(user_pubkey=CAROL, bunker_pubkey="c" * 64,
                    bunker_relays=["wss://bunker.example"], local_secret_hex="0" * 64,
                    display_name="Carol")
    sheet._store.upsert(other)
    sheet._account._switch(other)
    sheet._title_edit.setText("A new title")
    sheet._on_publish()
    assert asked[-1][:3] == (CAROL, "a-new-title", (RAW, COMPOSED))
    sheet.reject()
    typed = file_article(tmp_path, asked)
    typed._slug_edit.setText("my-own-name")
    typed._on_slug_edited("my-own-name")
    typed._on_publish()
    assert asked[-1][1:3] == ("my-own-name", ())


def test_the_link_name_goes_back_to_the_document_only_once_published(tmp_path, captured):
    """Review round 3, M2: Cancel wrote a link name back to the document
    (and marked it changed), one chosen only because no relay answered.
    The link name and the first publication date go back only as the
    record of a publication; Cancel leaves them as they were."""
    from nostr.publisher import FOUND, FirstPublication
    asked = []
    sheet = file_article(tmp_path, asked)
    sheet._slug_edit.setText("typed-name")
    sheet._on_slug_edited("typed-name")
    sheet.reject()
    assert not sheet.details_changed() and sheet.details().identifier == ""
    found = file_article(tmp_path, asked)
    found._on_publish()
    asked[-1][3](FirstPublication(FOUND, FIRST, under=RAW))
    assert not found.details_changed()                   # not published yet
    found._on_completed([("wss://nos.lol", True, "")])
    assert found.details().identifier == RAW and found.details().published_at == FIRST
    assert found.details_changed()


def test_a_file_this_computer_published_keeps_the_name_it_went_out_under(tmp_path):
    """Review round 3, M1: this computer's record is asked about the file's
    own name in both forms, and gives back the one it holds, as it is."""
    from main_window import MainWindow
    from nostr.first_publications import FirstPublications
    record = FirstPublications(tmp_path / "first.json")
    window = types.SimpleNamespace(_first_publications=record)
    path = f"/notes/{RAW}.md"
    assert MainWindow._link_name_for_file(window, path, PK) == MADE
    record.remember(PK, COMPOSED, FIRST)
    assert MainWindow._link_name_for_file(window, path, PK) == COMPOSED
    record.remember(PK, RAW, FIRST)
    assert MainWindow._link_name_for_file(window, path, PK) == RAW


@pytest.mark.parametrize("make", [article, note])
@pytest.mark.parametrize("language", ["en", "de"])
def test_a_sheet_on_the_smallest_window_scrolls_and_keeps_its_buttons(
        tmp_path, make, language):
    """Review round 2: at the main window's smallest size (325x189 on a
    Mac) Publish as Note was smaller than it could be drawn: its words
    and "Publishing as" were cut, and Cancel and Publish lost their
    labels. What the sheet holds scrolls; the account and the buttons
    always show whole."""
    import i18n
    from PySide6.QtWidgets import QMainWindow, QScrollArea
    i18n.install(language)
    try:
        window = QMainWindow()
        window.setMinimumSize(1, 1)
        window.resize(325, 189)
        window.show()
        sheet = make(tmp_path, parent=window)
        sheet.show()
        QApplication.processEvents()
        assert sheet.height() <= window.height() and sheet.width() <= window.width()
        for control in (sheet._cancel_btn, sheet._publish_btn, sheet._account):
            assert control.isVisible()
            assert control.height() >= control.minimumSizeHint().height()
            assert control.width() >= control.minimumSizeHint().width()
        scroll = sheet.findChild(QScrollArea)
        assert scroll is not None and scroll.isAncestorOf(
            sheet._title_edit if make is article else sheet._text_edit)
        sheet.close()
        window.close()
    finally:
        i18n.install(i18n.ENGLISH)


def _caret_in_view(sheet, widget):
    """Whether ``widget``'s caret (its whole self, without one) can be seen
    in the sheet's scrolling part."""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QScrollArea
    viewport = sheet.findChild(QScrollArea).viewport()
    caret = widget.inputMethodQuery(Qt.InputMethodQuery.ImCursorRectangle)
    caret = caret.toRect() if hasattr(caret, "toRect") else caret
    if caret.isNull() or not caret.isValid():
        caret = widget.rect()
    return viewport.rect().contains(QRect(widget.mapTo(viewport, caret.topLeft()),
                                          caret.size()))


@pytest.mark.parametrize("make", [article, note])
def test_the_field_being_typed_in_is_always_in_view(tmp_path, make):
    """Review round 3, low 4: in the smallest window the note box had the
    focus with none of it in view, the Title showed a quarter of itself,
    and Tab from Publish went round to the Title without scrolling to it.
    The field with the focus is scrolled into view, at the opening and
    whenever the focus moves."""
    from PySide6.QtWidgets import QMainWindow, QScrollArea
    window = QMainWindow()
    window.setMinimumSize(1, 1)
    window.resize(325, 189)
    window.show()
    sheet = make(tmp_path, parent=window)
    sheet.show()
    sheet.activateWindow()
    QApplication.processEvents()
    first = sheet._title_edit if make is article else sheet._text_edit
    first.setFocus()
    QApplication.processEvents()
    assert _caret_in_view(sheet, first)
    bar = sheet.findChild(QScrollArea).verticalScrollBar()
    assert bar.maximum() > 0                       # it does scroll there
    bar.setValue(bar.maximum())                    # read to the end ...
    sheet._cancel_btn.setFocus()
    assert QApplication.focusWidget() is sheet._cancel_btn
    QApplication.processEvents()
    first.setFocus(Qt.FocusReason.TabFocusReason)  # ... and Tab goes round
    QApplication.processEvents()
    assert _caret_in_view(sheet, first)
    sheet.close()
    window.close()


def test_the_app_calls_its_publish_windows_sheets():
    """Review round 2: a few places still spoke of a publish dialog. The
    app's own code and its documentation are read, nothing else (review
    round 3: a virtual environment or a build folder in the checkout)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    files = [*root.glob("*.py"), *(root / "nostr").rglob("*.py"),
             *(root / "spelling").rglob("*.py"), *root.glob("*.md"),
             *(root / "docs").rglob("*.md")]
    assert len(files) > 100
    named = [str(path.relative_to(root)) for path in files
             if "publish dialog" in path.read_text(encoding="utf-8").lower()]
    assert named == []


@pytest.mark.parametrize("from_file", [False, True])
def test_the_alert_claims_nothing_of_the_relays_that_may_be_untrue(
        tmp_path, captured, monkeypatch, from_file):
    """Review round 4: the alert said "None of the relays you publish to
    answered", but nobody can tell also when they answered and no index
    did, or when an answer could not be checked. It says what holds in
    every such case."""
    from nostr.publisher import UNKNOWN, FirstPublication
    alerts = []
    monkeypatch.setattr(article_module, "ask", lambda *a, **k: alerts.append(k) or "cancel")
    asked = []
    sheet = (file_article(tmp_path, asked) if from_file else article(
        tmp_path, default_slug="my-post", published_at_lookup=lambda a, s, done: asked.append(
            (a, s, (), done))))
    sheet._on_publish()
    asked[-1][3](FirstPublication(UNKNOWN))
    message = alerts[0]["message"]
    assert message.startswith("The relays gave no clear answer.") and "answered" not in message
