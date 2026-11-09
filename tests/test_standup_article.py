# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An article of a tab as a submission (MainWindow._submission_of).

Pinned here: a picture on this computer only is written by its asset
key and handed over with its bytes, to be encrypted with the text (it
never goes to the media servers in the clear, and nothing waits for an
upload first); one on the media servers already stays its address; a
cover on this computer becomes a picture of the submission and its
small cover; the article's identifier, details and co-authors are
those Article Details has; a picture of the private library is named,
as the editorial team could not open it. Once submitted, the tab keeps
the work's identifier, so a new title makes no other work."""

from __future__ import annotations

import inspect
import types

import pytest

from PySide6.QtGui import QPixmap

import tab_details
from main_window import MainWindow
from nostr.article_details import ArticleDetails
from nostr.media.media_visibility import PRIVATE, PUBLIC
from tests.test_media_document import GIF_BYTES, PNG_BYTES, _editor_with, _Window

CO_AUTHOR = ("c" * 64, "wss://relay.example")


class _Submitting(_Window):
    _submission_of = MainWindow._submission_of
    _preview_name = "Untitled"

    def __init__(self, cache_dir, details: ArticleDetails, private=()) -> None:
        super().__init__(cache_dir)
        self._details = details
        self._media_visibility = types.SimpleNamespace(
            state_of=lambda sha: PRIVATE if sha in private else PUBLIC)

    def _article_details_of(self, _ed) -> ArticleDetails:
        return self._details

    def _link_name_for_file(self, _path, _author) -> str:
        return ""


PROFILE = types.SimpleNamespace(user_pubkey="a" * 64)


def test_a_local_picture_goes_with_the_text_by_its_key_and_an_uploaded_one_by_address(
        tmp_path):
    win = _Submitting(tmp_path / "cache", ArticleDetails(
        identifier="ein-jahr", title="Ein Jahr", summary="Kurz", hashtags=("bitcoin",),
        coauthors=(CO_AUTHOR,), published_at=1700000000,
        carried=(("l", "de", "ISO-639-1"),)))
    hosted, local = win.uploaded_asset(alt="hosted"), win.local_asset(alt="sketch")
    ed = _editor_with(hosted, local, text="Look:")
    draft, _cover, pictures, private, public = win._submission_of(ed, PROFILE,
                                                                   win._uploads_needed(ed))
    assert hosted.remote_url in draft.content and local.key in draft.content
    assert [(p.key, p.data, p.alt) for p in draft.pictures] == [
        (local.key, GIF_BYTES, "sketch")]
    assert win._uploader.calls == []                # nothing went up in the clear
    assert (draft.d, draft.title, draft.summary, draft.tags, draft.authors,
            draft.published_at) == ("ein-jahr", "Ein Jahr", "Kurz", ("bitcoin",),
                                    (CO_AUTHOR,), 1700000000)
    assert pictures == 2 and private == []
    assert draft.lang == "de"                      # the article's own label (RG-M2b 11)
    assert public == 1                             # the hosted one stays a public link


def test_a_cover_on_this_computer_is_a_picture_of_the_submission_and_its_small_cover(
        tmp_path):
    win = _Submitting(tmp_path / "cache", ArticleDetails(title="T"))
    cover = win._asset_manager.adopt_bytes(PNG_BYTES, alt="")
    win._details = ArticleDetails(title="T", image=cover.key)
    ed = _editor_with(text="Just words.")
    draft, pixmap, pictures, _private, public = win._submission_of(ed, PROFILE,
                                                                    win._uploads_needed(ed))
    assert draft.image == cover.key and [p.key for p in draft.pictures] == [cover.key]
    assert draft.thumb is not None and draft.thumb["mime"] == "image/jpeg"
    assert isinstance(pixmap, QPixmap) and not pixmap.isNull()
    assert pictures == 0 and public == 0           # the cover goes encrypted
    assert draft.d                                  # a title makes the identifier


def test_a_picture_of_the_private_library_is_named(tmp_path):
    win = _Submitting(tmp_path / "cache", ArticleDetails(title="T"))
    hosted = win.uploaded_asset(alt="Café")
    win._media_visibility = types.SimpleNamespace(
        state_of=lambda sha: PRIVATE if sha == hosted.sha256 else PUBLIC)
    ed = _editor_with(hosted)
    *_rest, private, public = win._submission_of(ed, PROFILE, win._uploads_needed(ed))
    assert private == ["Café"]                     # as the picture is called, never its key
    assert public == 0                             # private: never counted as public


class _Kept(_Submitting):
    """The tab's own details, as the window keeps them (tab_details)."""

    _article_details_of = MainWindow._article_details_of
    _set_article_details = MainWindow._set_article_details
    _keep_submitted_identifier = MainWindow._keep_submitted_identifier

    def __init__(self, cache_dir) -> None:
        super().__init__(cache_dir, ArticleDetails())
        self.open_tabs = []

    def _published_article_of(self, _ed):
        return None

    def _editor_widget_index(self, ed) -> int:
        return 0 if ed in self.open_tabs else -1

    def _on_article_changed(self, _ed) -> None:
        pass


def test_a_submitted_work_keeps_its_identifier_when_its_title_changes(tmp_path):
    # RG-M2b 8: the identifier was the title's slug, made again each time.
    win = _Kept(tmp_path / "cache")
    ed = _editor_with(text="Just words.")
    win.open_tabs.append(ed)
    win._set_article_details(ed, ArticleDetails(title="Ein Jahr"))
    draft, *_rest = win._submission_of(ed, PROFILE, win._uploads_needed(ed))
    assert draft.d == "ein-jahr"
    win._keep_submitted_identifier(ed, draft.d)
    win._set_article_details(ed, win._article_details_of(ed).with_changes(title="Zwei Jahre"))
    again, *_rest = win._submission_of(ed, PROFILE, win._uploads_needed(ed))
    assert (again.d, again.title) == ("ein-jahr", "Zwei Jahre")
    closed = _editor_with(text="Gone.")
    win._keep_submitted_identifier(closed, "gone")    # its tab was closed meanwhile
    assert tab_details.details_of(closed) is None


class _Details:
    """The parts of MainWindow that fill Article Details."""

    _fill_article_details_panel = MainWindow._fill_article_details_panel

    def __init__(self, writer, details: ArticleDetails) -> None:
        self.shown = []
        self._article_details_panel = types.SimpleNamespace(
            isVisible=lambda: True, set_derived_title=lambda _title: None,
            show_article=lambda _details, **kw: self.shown.append(kw))
        self.act_article_details = types.SimpleNamespace(isChecked=lambda: True)
        self._ed = types.SimpleNamespace()
        self._profile_store = types.SimpleNamespace(default=lambda: PROFILE)
        self._standup_writer = writer
        self._details = details

    def current_editor(self):
        return self._ed

    def current_path(self):
        return None

    def _published_article_of(self, _ed):
        return None

    def _article_details_of(self, _ed) -> ArticleDetails:
        return self._details

    def _link_name_for_file(self, _path, _author) -> str:
        return ""

    def _derived_title_of(self, _ed) -> str:
        return ""


@pytest.mark.parametrize("identifier, writer, submitted", [
    ("ein-jahr", True, True),
    ("anders", True, False),
    ("ein-jahr", False, False),                     # nothing submitted here yet
], ids=["submitted", "another text", "no writer"])
def test_article_details_fixes_the_link_name_of_a_submitted_text(identifier, writer,
                                                                 submitted):
    # RG-M2r 10: a hand-edited link name made the next Submit another sid,
    # a second submission of the same text.
    asked = []

    def has_writer_entry(profile, d):
        asked.append((profile, d))
        return d == "ein-jahr"

    win = _Details(types.SimpleNamespace(has_writer_entry=has_writer_entry) if writer else None,
                   ArticleDetails(identifier=identifier, title="Ein Jahr"))
    win._fill_article_details_panel()
    assert win.shown[-1]["submitted"] is submitted
    assert asked == ([(PROFILE, identifier)] if writer else [])
    # The writer's list read, or a recorded entry, shows it again.
    assert "listed.connect" in inspect.getsource(MainWindow._standup)


def test_a_submitted_text_whose_link_name_was_kept_shows_it_fixed_at_once(tmp_path):
    win = _Kept(tmp_path / "cache")
    ed = _editor_with(text="Just words.")
    win.open_tabs.append(ed)
    win._set_article_details(ed, ArticleDetails(title="Ein Jahr", identifier="ein-jahr"))
    filled = []
    win.current_editor = lambda: ed
    win._fill_article_details_panel = lambda: filled.append(True)
    win._keep_submitted_identifier(ed, "ein-jahr")  # unchanged: nothing else refreshes it
    assert filled == [True]


def test_the_submit_command_keeps_the_identifier_its_sheet_sent(tmp_path, monkeypatch):
    # The production caller: Nostr > Submit to a Publication.
    import main_window
    from PySide6.QtCore import QObject, Signal

    class Sheet(QObject):
        submitted = Signal(str)
        opened = []

        def __init__(self, **kw) -> None:
            super().__init__()
            self.draft = kw["draft"]
            self.public = kw["public_pictures"]

        def setAttribute(self, *_args) -> None:  # noqa: N802, Qt's name
            pass

        def open(self) -> None:
            Sheet.opened.append(self)

    monkeypatch.setattr(main_window, "SubmitSheet", Sheet)
    win = _Kept(tmp_path / "cache")
    ed = _editor_with(text="Just words.")
    win.open_tabs.append(ed)
    win._set_article_details(ed, ArticleDetails(title="Ein Jahr"))
    win._profile_store = types.SimpleNamespace(default=lambda: types.SimpleNamespace(
        user_pubkey="a" * 64, display_name="Anna"))
    win.current_editor = lambda: ed
    win._wants_article_header = lambda _ed: True
    win._standup = lambda: None
    win._avatars = win._avatar_batcher = types.SimpleNamespace(request=None)
    win.is_dark_theme = False
    MainWindow._on_nostr_submit_publication(win)
    sheet, = Sheet.opened
    assert sheet.public == 0                       # the count reaches the sheet
    sheet.submitted.emit(sheet.draft.d)
    assert tab_details.details_of(ed).identifier == "ein-jahr"
