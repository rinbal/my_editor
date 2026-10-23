# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish as Article: a sheet on the document's window that publishes it
as a long-form article (NIP-23, kind 30023).

    ┌──────────────┐  Title:        [Fafuui S s                  ]
    │   [ cover ]  │  Summary:      [One sentence                ]
    │ Fafuui S s   │  Hashtags:     (#bitcoin ×) (#essay ×)  Add a…
    │ One sentence │  Cover image:  [picture]  Choose…  Remove
    │ DrShift ·    │  People:       (DrShift mentioned) Add Co-Author…
    │ 1 min read   │                ▸ Show Details
    └──────────────┘
    Publishing as (D) DrShift ▾                      [Cancel] [Publish]

What is published is the document's text, as it is written: there is no
second copy of it to edit here. The card at the leading edge shows the
article the way readers see it in their lists (its cover, title, summary,
the author and the reading time) as the fields change.

The fields are the article's details, the one model Article Details
shows too (nostr/article_details.py): what is changed here goes back to
the document, as there. The title starts as the article's own, else as
its first heading or first line in plain words, a mention as the
person's name (article_details.default_title). Hashtags are tags
(tag_field.py), the cover a picture well (cover_field.py), and People
everyone the article will tag, read from the event the publisher builds:
those the text mentions or quotes, and the co-authors, added and removed
as in Article Details (coauthors.py).

Show Details holds the article's link name (its identifier, the ``d``
tag, made from the title until it is typed) with how it ends the
article's address, the cover's web address, and when the article first
went out. An article that has its link name already (a draft's, a file's
or one published before) keeps it as the title changes: another would
publish another article. A key in the link name is said under it, with
Publish dimmed, before anything is sent.

Publishing asks the relays for the article's first publication date
first (an edit keeps it), unless this computer or the draft knows it;
when no relay can tell, the person decides. A file's article whose link
name is still the one made for it is asked about under the file's own
name too (3.3 published files under it), in the same question: found
there, it goes out under that name. Nothing is asked before Publish.
Then the event goes through the secret key check and the publish
pipeline. The link name and the first publication date go back to the
document only once it is published, as the record of that.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QDateTime, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout, QDialog, QDialogButtonBox, QFormLayout, QFrame, QLabel,
    QLineEdit, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

import announce
from alerts import CANCEL, DEFAULT, NORMAL, Button, ask, answer_return_as_the_system_does
from constants import (
    DARK_BORDER, DARK_MENU_BG, DARK_MUTED_FG, LIGHT_BG, LIGHT_BORDER, LIGHT_MUTED_FG,
)
from i18n import _, format_moment, ngettext

from ..article_details import ArticleDetails
from ..avatar_store import AvatarStore
from ..bech32 import encode_naddr
from ..blossom.store import MediaStore
from ..bunker import BunkerSessionPool, humanize_failure
from ..first_publications import FirstPublications
from ..known_people import KnownPeople
from ..media.media_visibility import MediaVisibility
from ..media.private_library import PrivateLibrary
from ..media.publish_copy import PublicCopyMaker
from .media_saver import MediaSaver
from ..nip05_check import Nip05Check
from ..outbox import RelayDirectory, relays_from
from ..preview import reading_minutes
from ..profiles import Profile, ProfileStore
from ..publisher import (
    FOUND, NEVER, FirstPublication, PublishedMedia, PublishJob, PublishResult, build_article,
    file_link_names, find_first_publication, link_name, slugify,
)
from ..relay import RelayPool
from ..search import Nip50SearchClient
from .coauthors import Coauthors
from .cover_field import CoverImageField
from .mention_picker import MentionPicker
from . import appearance
from .publish_sheet import (
    AccountRow, BottomRow, PeopleRow, StatusLine, fit_margins, fit_to_window, make_sheet,
    people_tagged, scroll_area, sheet_stylesheet,
)
from .secret_key_check import publishable, slug_problem
from .tag_field import TagField

# The sheet's width, as the window has room for it.
_WIDTH = 760


class _ArticleCard(QFrame):
    """The article as readers see it in a list: cover, title, summary, and
    the author with the reading time."""

    WIDTH = 220
    COVER_HEIGHT = 124

    def __init__(self, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("article_card")
        self.setAccessibleName(_("Preview"))
        self.setFixedWidth(self.WIDTH)
        self.set_dark(is_dark)
        column = QVBoxLayout(self)
        column.setContentsMargins(1, 1, 1, 12)
        column.setSpacing(6)
        self.cover = QLabel()
        self.cover.setObjectName("card_cover")
        self.cover.setFixedSize(self.WIDTH - 2, self.COVER_HEIGHT)
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.cover)
        self.title = self._words("card_title")
        self.summary = self._words("card_summary")
        self.byline = self._words("card_byline")
        for label in (self.title, self.summary, self.byline):
            label.setContentsMargins(12, 0, 12, 0)
            column.addWidget(label)
        column.addStretch(1)

    def set_dark(self, is_dark: bool) -> None:
        appearance.neutral(self, is_dark)
        border, ground, muted = ((DARK_BORDER, DARK_MENU_BG, DARK_MUTED_FG) if is_dark
                                 else (LIGHT_BORDER, LIGHT_BG, LIGHT_MUTED_FG))
        self.setStyleSheet(
            f"QFrame#article_card {{ background: {ground}; border: 1px solid {border};"
            f" border-radius: 8px; }}"
            f"QLabel#card_cover {{ background: {border}; border: none;"
            f" border-top-left-radius: 7px; border-top-right-radius: 7px; }}"
            f"QLabel#card_title {{ color: {self.palette().color(self.foregroundRole()).name()}; font-size: 15px; font-weight: 600; background: transparent; }}"
            f"QLabel#card_summary, QLabel#card_byline {{ color: {muted};"
            f" background: transparent; }}"
            f"QLabel#card_byline {{ font-size: 11px; }}"
            f"QLabel#card_title[untitled=\"true\"] {{ color: {muted}; }}")

    @staticmethod
    def _words(name: str) -> QLabel:
        label = QLabel()
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        return label

    def show_article(self, title: str, summary: str, picture: QPixmap, byline: str) -> None:
        room = self.WIDTH - 26
        self.title.setProperty("untitled", "false" if title else "true")
        self.title.style().unpolish(self.title)
        self.title.style().polish(self.title)
        self.title.setText(_elided(self.title, title or _("Untitled"), room, 3))
        self.summary.setText(_elided(self.summary, summary, room, 3))
        self.summary.setVisible(bool(summary))
        self.byline.setText(byline)
        if picture.isNull():
            self.cover.setPixmap(QPixmap())
        else:
            size = self.cover.size()
            filled = picture.scaled(size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
            self.cover.setPixmap(filled.copy((filled.width() - size.width()) // 2,
                                             (filled.height() - size.height()) // 2,
                                             size.width(), size.height()))
        self.setAccessibleDescription(". ".join(part for part in (
            title or _("Untitled"), summary, byline) if part))
        # As tall as its words take at its own width: a layout would ask
        # its height at the width it offers (under the fields, the sheet's),
        # where the words take fewer lines, and the fields would lose the rest.
        self.setFixedHeight(self.layout().totalHeightForWidth(self.WIDTH))


def _elided(label: QLabel, text: str, width: int, lines: int) -> str:
    """``text`` cut after a word, with "…", to fit ``lines`` lines of
    ``width`` in ``label``'s font."""
    metrics = QFontMetrics(label.font())
    room = metrics.lineSpacing() * lines

    def fits(words: str) -> bool:
        return metrics.boundingRect(0, 0, width, 10_000, Qt.TextFlag.TextWordWrap,
                                    words).height() <= room

    if fits(text):
        return text
    words = text.split(" ")
    while len(words) > 1 and not fits(" ".join(words) + "…"):
        words.pop()
    shown = " ".join(words)
    while shown and not fits(shown + "…"):
        shown = shown[:-1]
    return shown.rstrip() + "…"


# A form's labels as the platform sets them: on a Mac at the trailing edge
# of their column, beside their fields (Fusion, which the app uses
# everywhere, would put them at the leading edge); elsewhere at the leading
# edge. Beside the field's first line either way.
_LABEL_ALIGNMENT = ((Qt.AlignmentFlag.AlignRight if sys.platform == "darwin"
                     else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignTop)


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setLabelAlignment(_LABEL_ALIGNMENT)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setHorizontalSpacing(10)
    form.setVerticalSpacing(10)
    return form


def _label(text: str, buddy: QWidget) -> QLabel:
    """A field's label: its words with a colon, beside the field's first
    line."""
    label = QLabel(text)
    label.setBuddy(buddy)
    label.setAlignment(_LABEL_ALIGNMENT)
    label.setContentsMargins(0, 4, 0, 0)
    return label


class PublishArticleDialog(QDialog):
    """Publish the document as an article (see the module docstring).

    ``published(naddr, results)`` once a relay took it; the sheet then
    closes. A failure is shown in it, to try again. ``details()`` and
    ``details_changed()`` give what the person changed of the article's
    details, for the document."""

    # Args: (naddr_string, list[PublishResult])
    published = Signal(str, list)

    def __init__(
        self,
        *,
        body_markdown: str,
        active_profile: Profile,
        store: ProfileStore,
        relay_pool: RelayPool,
        relay_directory: RelayDirectory,
        session_pool: BunkerSessionPool,
        entitled_relays: Optional[Callable[[], Sequence[str]]] = None,
        known_people: KnownPeople,
        search_client: Nip50SearchClient,
        avatars: AvatarStore,
        address_check: Optional[Nip05Check] = None,
        can_receive_zaps: Optional[Callable[[str, str, Callable], None]] = None,
        media_store: Optional[MediaStore] = None,
        media_visibility: Optional[MediaVisibility] = None,
        copy_maker: Optional[PublicCopyMaker] = None,
        private_library: Optional[PrivateLibrary] = None,
        media_saver: Optional[MediaSaver] = None,
        default_title: str = "",
        default_slug: str = "",
        file_name: str = "",
        first_published: Optional[int] = None,
        details: Optional[ArticleDetails] = None,
        first_publications: Optional[FirstPublications] = None,
        published_at_lookup: Optional[
            Callable[[str, str, Callable[[FirstPublication], None]], None]] = None,
        media: Sequence[PublishedMedia] = (),
        parent=None,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Publish as Article"))
        make_sheet(self)
        self.setMinimumSize(420, 320)

        # What is published: the document's text, as written.
        self._body = body_markdown.strip()
        self._store = store
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        # Relays this account has standing on beyond its own list, resolved
        # when the publish actually happens rather than at the sheet's opening.
        self._entitled_relays = entitled_relays
        self._known_people = known_people
        self._search_client = search_client
        self._avatars = avatars
        self._address_check = address_check
        self._media_store = media_store
        # A cover image is the most public thing in an article: it is the
        # picture every reader sees before they read a word. So the same
        # gate the editor uses runs here, and a picker with no visibility
        # wired simply sees every file as public.
        self._media_visibility = media_visibility or MediaVisibility()
        self._copy_maker = copy_maker
        # Held so the cover picker can start the library reading; nothing
        # is read on the way into the sheet (its signer prompts belong to
        # the moment someone goes looking for a picture).
        self._private_library = private_library
        # The window's Save As, for the cover picker (CoverImageField).
        self._media_saver = media_saver
        self._is_dark = is_dark
        self._job: Optional[PublishJob] = None
        self._signed_event_id: Optional[str] = None
        # NIP-23: ``published_at`` is when the article first went out, and
        # an edit keeps it. Known when the tab came from a draft that
        # carries it (for that draft's identifier) or when this computer
        # published the article before; otherwise asked of the relays at
        # publish time. "Now" only for a first publication; when nobody
        # can tell, the person decides (never a guess).
        self._first_published = (
            {default_slug: first_published} if default_slug and first_published else {})
        self._first_publications = first_publications
        # ``(author, slug, on_done)``; for a file's article also ``also=``,
        # the file's own names (find_first_publication).
        self._published_at_lookup = published_at_lookup or self._look_up_published_at
        self._looking_up = False
        # The link name and first publication date being published with,
        # and once a relay took it, published with: only then do they go
        # back to the document (details()).
        self._publishing: Optional[Tuple[str, int]] = None
        self._published_as: Optional[Tuple[str, int]] = None
        # The article's details (an imported article's summary, cover,
        # hashtags and source, its co-authors): offered here, published
        # with it, and what changes here goes back to the document.
        self._details = details or ArticleDetails()
        # What is known of the pictures, audio and video in the text, for
        # their descriptions (NIP-92 imeta).
        self._media = tuple(media)
        # The link name follows the title until it is typed. An article
        # that has one already (its draft's, its file's) keeps it, and so
        # does one this computer published under it: another one would
        # publish another article.
        self._slug_is_auto = not (
            self._details.identifier or self._first_published
            or "identifier" in self._details.kept_fields
            or (first_publications is not None and default_slug
                and first_publications.get(active_profile.user_pubkey, default_slug)))
        # The link name it has already, kept letter for letter (link_name).
        self._kept_slug = default_slug
        self._coauthor_picker: Optional[MentionPicker] = None
        # A file's article with no link name of its own, starting with the
        # one made of the file's name: its file's names as they are (as the
        # file system gives it, and composed), which version 3.3 published
        # files under and recorded nothing of, nor read any front matter
        # (a date there says nothing of the name). Asked about at Publish
        # with the made link name (_on_publish), never before: nothing goes
        # out before the person publishes.
        own_names = file_link_names(file_name)
        self._file_names = (own_names if own_names and not self._details.identifier
                            and default_slug not in own_names else [])
        self._slug_typed = False

        self._build(active_profile, default_title, default_slug)
        self._coauthors = Coauthors(profile=self._account.profile,
                                    relay_hint=relay_directory.relay_hint,
                                    can_receive_zaps=can_receive_zaps)
        self._summary_edit.setText(self._details.summary)
        self._image_edit.setText(self._details.image)
        self._hashtags.set_tags(self._details.hashtags)
        for edit in self._kept_edits():        # those the file keeps: read-only
            edit.setReadOnly(True)
        self._cover.setReadOnly("image" in self._details.kept_fields)
        # What it showed at first: only what changes here goes back.
        self._details_shown = self.details()
        self._show_people()
        self._refresh_card()
        self._refresh_publish_enabled()
        self._fit(_WIDTH)

    # -- the sheet ----------------------------------------------------------------

    def _build(self, profile: Profile, default_title: str, default_slug: str) -> None:
        self.setStyleSheet(sheet_stylesheet(self._is_dark))
        appearance.surface(self, self._is_dark)
        appearance.bind(self)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(10)

        # Everything but the account and the buttons scrolls, the heading
        # too: in the smallest window there is room for little more.
        content = QWidget()
        page = QVBoxLayout(content)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(10)
        heading = QLabel(_("Publish as Article"))
        heading.setObjectName("title")
        page.addWidget(heading)
        self._columns = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        page.addLayout(self._columns, 1)
        self._columns.setContentsMargins(0, 0, 4, 0)
        self._columns.setSpacing(20)
        self._card = _ArticleCard(self._is_dark)
        self._columns.addWidget(self._card, 0, Qt.AlignmentFlag.AlignTop)
        # Made before the form, which names its account (the cover's
        # uploads are this account's), but placed after it in the sheet.
        self._account = AccountRow(profile, self._store, self._avatars,
                                   is_dark=self._is_dark)
        self._account.switched.connect(self._on_account_switched)
        self._columns.addLayout(self._build_form(default_title, default_slug), 1)
        self._scroll = scroll_area(content)
        root.addWidget(self._scroll, 1)

        self._status = StatusLine(self._is_dark)
        root.addWidget(self._status)

        buttons = QDialogButtonBox()
        self._cancel_btn = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._publish_btn = buttons.addButton(_("Publish"),
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._publish_btn.clicked.connect(self._on_publish)
        root.addWidget(BottomRow(self._account, buttons))
        # Tab goes through the fields, then the account, then the buttons.
        QWidget.setTabOrder(self._details_toggle, self._slug_edit)
        QWidget.setTabOrder(self._slug_edit, self._image_edit)
        QWidget.setTabOrder(self._image_edit, self._account.button)
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def _add_row(self, label: str, buddy: Optional[QWidget], field: QWidget) -> None:
        """A row of the form: ``label`` (none when empty) beside ``field``.
        The labels and fields are kept here, so the form's own items are
        never wrapped (Python would hold items the form deletes)."""
        if label:
            words = _label(label, buddy)
            self._labels.append(words)
            self._form.addRow(words, field)
        else:
            self._form.addRow("", field)
        self._fields.append(field)

    def _build_form(self, default_title: str, default_slug: str) -> QFormLayout:
        form = self._form = _form()
        self._labels: List[QLabel] = []
        self._fields: List[QWidget] = []

        self._title_edit = QLineEdit(default_title)
        self._title_edit.setPlaceholderText(_("Article title"))
        self._title_edit.setAccessibleName(_("Title"))
        self._title_edit.textChanged.connect(self._on_title_changed)
        self._add_row(_("Title:"), self._title_edit, self._title_edit)
        self._add_kept_note(form, "title")

        self._summary_edit = QLineEdit()
        self._summary_edit.setPlaceholderText(_("Short summary (one sentence)"))
        self._summary_edit.setAccessibleName(_("Summary"))
        self._summary_edit.textChanged.connect(self._refresh_card)
        self._add_row(_("Summary:"), self._summary_edit, self._summary_edit)
        self._add_kept_note(form, "summary")

        self._hashtags = TagField(is_dark=self._is_dark)
        self._hashtags.setAccessibleName(_("Hashtags"))
        self._add_row(_("Hashtags:"), self._hashtags, self._hashtags)
        self._add_kept_note(form, "hashtags")

        # The cover, chosen the same way as in Article Details; its web
        # address is under Show Details.
        self._cover = CoverImageField(
            media_store=self._media_store, media_visibility=self._media_visibility,
            copy_maker=self._copy_maker, private_library=self._private_library,
            profile=self._account.profile, saver=self._media_saver, is_dark=self._is_dark)
        self._cover.picture_changed.connect(self._refresh_card)
        # A cover on its way up is waited for: published now, it would be
        # missing.
        self._cover.uploading_changed.connect(lambda _up: self._refresh_publish_enabled())
        self._image_edit = self._cover.url_edit
        cover = QWidget()
        cover_column = QVBoxLayout(cover)
        cover_column.setContentsMargins(0, 0, 0, 0)
        cover_column.setSpacing(4)
        cover_column.addWidget(self._cover)
        self._cover_problem = self._problem_label()
        cover_column.addWidget(self._cover_problem)
        self._add_kept_note(cover_column, "image")
        self._cover.problem.connect(self._say_cover_problem)
        self._cover.changed.connect(lambda _text: self._cover_problem.hide())
        self._add_row(_("Cover image:"), self._cover.pick_button or self._cover.preview, cover)

        self._add_coauthor = QPushButton(_("Add Co-Author…"))
        self._add_coauthor.clicked.connect(self._ask_coauthor)
        self._people = PeopleRow(self._known_people, self._avatars,
                                 add_button=self._add_coauthor)
        self._people.setAccessibleName(_("People"))
        self._people.remove_requested.connect(self._remove_coauthor)
        self._add_row(_("People:"), self._add_coauthor, self._people)

        self._details_toggle = QToolButton()
        self._details_toggle.setObjectName("disclosure")
        self._details_toggle.setCheckable(True)
        self._details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._details_toggle.toggled.connect(self._show_details)
        self._add_row("", None, self._details_toggle)

        # Show Details: the link name, the cover's address, the first date.
        self._slug_edit = QLineEdit()
        self._slug_edit.setPlaceholderText(_("my-article"))
        self._slug_edit.setAccessibleName(_("Link name"))
        self._slug_edit.setText(default_slug or (slugify(default_title) if default_title
                                                 else ""))
        self._slug_edit.textEdited.connect(self._on_slug_edited)
        self._slug_edit.textChanged.connect(self._show_link)
        link = QWidget()
        link_column = QVBoxLayout(link)
        link_column.setContentsMargins(0, 0, 0, 0)
        link_column.setSpacing(3)
        link_column.addWidget(self._slug_edit)
        # How it ends the article's address.
        self._slug_hint = QLabel()
        self._slug_hint.setObjectName("help")
        self._slug_hint.setTextFormat(Qt.TextFormat.PlainText)
        link_column.addWidget(self._slug_hint)
        # Why the link name can't be published, under it.
        self._slug_problem = self._problem_label()
        link_column.addWidget(self._slug_problem)
        self._add_kept_note(link_column, "identifier")
        kept = QLabel(_("A saved or published article keeps its link name; publishing it "
                        "under another one makes another article."))
        kept.setObjectName("help")
        kept.setWordWrap(True)
        kept.setVisible(bool(self._details.identifier))
        link_column.addWidget(kept)
        self._link_row = form.rowCount()
        self._add_row(_("Link name:"), self._slug_edit, link)

        self._image_edit.setAccessibleName(_("Cover image address"))
        self._image_edit.show()
        self._add_row(_("Cover image address:"), self._image_edit, self._image_edit)

        when = self._details.published_at
        first = QLabel(_("First published {moment}").format(
            moment=format_moment(QDateTime.fromSecsSinceEpoch(int(when))))
            if when else _("Not published yet."))
        first.setObjectName("help")
        first.setWordWrap(True)
        self._add_row("", None, first)
        # The fields stay where they are when Show Details adds its rows.
        widest = max(label.sizeHint().width() for label in self._labels)
        for label in self._labels:
            label.setMinimumWidth(widest)
        self._show_details(False)
        self._show_link()
        return form

    @staticmethod
    def _problem_label() -> QLabel:
        label = QLabel()
        label.setObjectName("error")
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.hide()
        return label

    def _wanted_height(self, width: int) -> int:
        """The sheet's height at ``width`` with every row shown that is
        shown now (rows of tags and people wrap)."""
        content = self._scroll.widget()
        content.layout().activate()
        margins = self.layout().contentsMargins()
        inner = width - margins.left() - margins.right()
        needed = (content.heightForWidth(inner) if content.hasHeightForWidth()
                  else content.sizeHint().height())
        around = self.sizeHint().height() - self._scroll.sizeHint().height()
        return around + needed + 8

    def _fit(self, width: int) -> None:
        """As tall as what it shows needs, as much as its window has."""
        fit_to_window(self, QSize(width, max(self.height() if self.isVisible() else 0,
                                             self._wanted_height(width))))

    def _show_details(self, shown: bool) -> None:
        self._details_toggle.setText(_("Hide Details") if shown else _("Show Details"))
        self._details_toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                          else Qt.ArrowType.RightArrow)
        if self._details_toggle.isChecked() != shown:
            self._details_toggle.setChecked(shown)
            return                                  # toggled: shown from there
        for row in range(self._link_row, self._form.rowCount()):
            self._form.setRowVisible(row, shown)
        if self.isVisible():
            self._fit(self.width())                 # room for them

    def apply_theme(self, is_dark: bool) -> None:
        """The app's appearance changed while the sheet is open: all of it
        takes the new one, as the window does."""
        self._is_dark = is_dark
        self.setStyleSheet(sheet_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        self._card.set_dark(is_dark)
        self._hashtags.set_dark(is_dark)
        self._cover.set_dark(is_dark)
        self._account.set_dark(is_dark)
        self._status.set_dark(is_dark)
        if self._coauthor_picker is not None:
            self._coauthor_picker.set_dark(is_dark)

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)
        # The card beside the fields while they have their room (longer
        # words take more of it), else under them; narrower still, each
        # label above its field.
        margins = self.layout().contentsMargins()
        room = (self.width() - margins.left() - margins.right()
                - self._scroll.verticalScrollBar().sizeHint().width()
                - self._columns.contentsMargins().right())
        fields = max(field.minimumSizeHint().width() for field in self._fields)
        form = self._labels[0].minimumWidth() + self._form.horizontalSpacing() + fields
        beside = room >= self._card.width() + self._columns.spacing() + form
        self._columns.setDirection(QBoxLayout.Direction.LeftToRight if beside
                                   else QBoxLayout.Direction.BottomToTop)
        wrap = room < form
        self._form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows if wrap
                                    else QFormLayout.RowWrapPolicy.DontWrapRows)
        for label in self._labels:
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop if wrap
                               else _LABEL_ALIGNMENT)

    def showEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().showEvent(event)
        self._fit(self.width())         # its words measured as shown now
        if not self._slug_problem.isHidden():
            # Said before the sheet was there to say it from: again now.
            announce.announce(self._slug_edit, self._slug_problem.text())

    # -- what the card shows ----------------------------------------------------------

    def _refresh_card(self, *_args) -> None:
        profile = self._account.profile()
        minutes = reading_minutes(self._body)
        byline = " · ".join((profile.display_name or profile.npub_short(),
                                  ngettext("{n} min read", "{n} min read",
                                           minutes).format(n=minutes)))
        self._card.show_article(self._title_edit.text().strip(),
                                self._summary_edit.text().strip(), self._cover.picture(),
                                byline)

    # -- title and link name -------------------------------------------------------------

    def _on_title_changed(self, text: str) -> None:
        if self._slug_is_auto:
            self._slug_edit.blockSignals(True)
            self._slug_edit.setText(slugify(text))
            self._slug_edit.blockSignals(False)
            self._show_link()
        self._refresh_card()
        self._refresh_publish_enabled()

    def _on_slug_edited(self, _text: str) -> None:
        # Typed: it no longer follows the title, and it is the person's.
        self._slug_is_auto = False
        self._slug_typed = True
        self._refresh_publish_enabled()

    def _slug(self) -> str:
        """The link name in the field, as it is published: one the article
        has already exactly as it is, one typed in one form (link_name)."""
        return link_name(self._slug_edit.text(), self._kept_slug)

    def _show_link(self, *_args) -> None:
        slug = self._slug()
        self._slug_hint.setText("…/" + slug if slug else "")
        self._slug_hint.setVisible(bool(slug))

    # -- people -----------------------------------------------------------------------

    def _show_people(self) -> None:
        """Everyone the article will tag, from the event it would be."""
        author = self._account.profile().user_pubkey
        event = build_article(
            self._body, author, self._slug() or "-",
            extra_tags=self._details.published_tags(self._body, author=(
                author, self._relay_directory.relay_hint(author))) or None)
        self._people.show_people(people_tagged(event, self._body, markdown=True))
        self._coauthors.ask(self._details.coauthors, self._zappable_answered)

    def _ask_coauthor(self) -> None:
        if self._coauthor_picker is None:
            self._coauthor_picker = MentionPicker(
                self._known_people, self._search_client, avatars=self._avatars, parent=self,
                is_dark=self._is_dark, address_check=self._address_check)
            self._coauthor_picker.picked.connect(self._add_coauthor_picked)
        self._coauthor_picker.open_at(self._add_coauthor.rect(), self._add_coauthor)

    def _add_coauthor_picked(self, person) -> None:
        details = self._coauthors.added(self._details, person)
        if details is not None:         # None: already there, or the author
            self._details = details
            self._show_people()

    def _remove_coauthor(self, pubkey: str) -> None:
        self._details = self._coauthors.removed(self._details, pubkey)
        self._show_people()
        self._add_coauthor.setFocus()

    def _zappable_answered(self, pubkey: str, able) -> None:
        details = self._coauthors.learned(self._details, pubkey, able)
        if details is not None:
            self._details = details

    # -- account ----------------------------------------------------------------------

    def _on_account_switched(self, _profile: Profile) -> None:
        self._show_people()
        self._refresh_card()

    # -- state ------------------------------------------------------------------------

    def details(self) -> ArticleDetails:
        """The article's details as the sheet has them now (the one model
        Article Details shows too): what it was given, with what was
        changed here. Its link name and first publication date are those
        it was published with, once a relay took it, and as they were
        until then: a link name typed, made or found here is never the
        document's because the sheet was closed (review round 3, M2)."""
        identifier, published_at = self._published_as or (self._details.identifier,
                                                           self._details.published_at)
        return self._details.with_changes(
            identifier=identifier,
            title=self._title_edit.text().strip(),
            summary=self._summary_edit.text().strip(),
            image=self._image_edit.text().strip(),
            hashtags=self._hashtags.tags(),
            published_at=published_at)

    def details_changed(self) -> bool:
        """Whether anything of :meth:`details` changed here (typed, or
        published with a first publication date)."""
        return self.details() != self._details_shown

    def _refresh_publish_enabled(self) -> None:
        slug = self._slug()
        # A key in the link name is said under it, before anything is sent
        # (not even the question whether the article went out before),
        # with Show Details open so the field it names can be seen.
        problem = slug_problem(slug) or ""
        announce.field_problem(self._slug_edit, self._slug_problem, problem)
        if problem:
            self._show_details(True)
        # NIP-23 requires the d-tag; an empty document has nothing to say;
        # a cover still uploading is waited for.
        self._publish_btn.setEnabled(self._job is None and not self._looking_up
                                     and bool(slug) and bool(self._body) and not problem
                                     and not self._cover.uploading())

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.say(text, error=error)

    def _say_cover_problem(self, reason: str) -> None:
        announce.field_problem(self._cover.preview, self._cover_problem, reason)

    def _add_kept_note(self, layout, field_name: str) -> None:
        """Under a detail the article's file keeps as written
        (ArticleDetails.kept): the line Article Details shows there too; in
        the form (``layout`` the form), a row of its own under the field."""
        if field_name in self._details.kept_fields:
            note = QLabel(_("Kept as written in the file."))
            note.setObjectName("help")
            note.setWordWrap(True)
            if layout is self._form:
                self._add_row("", None, note)
            else:
                layout.addWidget(note)

    def _kept_edits(self) -> set:
        """The fields of the details the file keeps as written: read-only."""
        edits = {"title": self._title_edit, "summary": self._summary_edit,
                 "identifier": self._slug_edit, "image": self._image_edit,
                 "hashtags": self._hashtags}
        return {edit for name, edit in edits.items() if name in self._details.kept_fields}

    def _set_busy(self, busy: bool) -> None:
        # Those the file keeps stay read-only; the cover lets nothing be
        # picked, dropped or removed meanwhile (CoverImageField.setBusy).
        kept = self._kept_edits()
        for edit in (self._title_edit, self._summary_edit, self._slug_edit):
            edit.setReadOnly(busy or edit in kept)
        self._hashtags.setEnabled(not busy)
        self._cover.setBusy(busy)
        self._people.setEnabled(not busy)
        self._account.setEnabled(not busy)
        self._refresh_publish_enabled()
        if busy:
            self._publish_btn.setEnabled(False)

    # -- publishing -------------------------------------------------------------------

    def _look_up_published_at(self, author: str, slug: str,
                              on_done: Callable[[FirstPublication], None],
                              also: Sequence[str] = ()) -> None:
        find_first_publication(self._relay_pool, self._relay_directory, author, slug,
                               on_done, parent=self, also=also)

    def _on_publish(self) -> None:
        slug = self._slug()
        if (not slug or not self._body or self._job is not None or self._looking_up
                or slug_problem(slug) or self._cover.uploading()):
            return
        author = self._account.profile().user_pubkey
        known = self._first_published.get(slug) or (
            self._first_publications.get(author, slug)
            if self._first_publications is not None else None)
        # A file's article with the link name made for it may be out under
        # the file's own name: asked about in the same question, now that
        # the person publishes, for the account and the link name of now,
        # also when a date is known (its front matter's says nothing of
        # the name).
        also = self._file_names_to_ask(author, slug)
        if known and not also:
            self._publish(slug, self._body, known)
            return
        # An article published before keeps its date: find it first.
        self._looking_up = True
        self._set_busy(True)
        self._set_status(_("Checking whether this article was published before…"))
        answer = (lambda when, s=slug, a=author, names=tuple(also), k=known:
                  self._on_published_at(s, a, when, names, k))
        if also:
            self._published_at_lookup(author, slug, answer, also=also)
        else:
            self._published_at_lookup(author, slug, answer)

    def _file_names_to_ask(self, author: str, slug: str) -> List[str]:
        """The file's own names to ask about with ``slug`` at Publish: none
        for a link name typed, or one this computer published the article
        under (that is the article)."""
        if not self._file_names or self._slug_typed or (
                self._first_publications is not None
                and self._first_publications.get(author, slug)):
            return []
        return [name for name in self._file_names if name != slug]

    def _on_published_at(self, slug: str, author: str, found: FirstPublication,
                         file_names: Sequence[str] = (), known: Optional[int] = None) -> None:
        if not self._looking_up:
            return                                     # the sheet was closed meanwhile
        self._looking_up = False
        if (author != self._account.profile().user_pubkey
                or slug != self._slug()):
            # Changed while asking (another account, another link name).
            self._set_busy(False)
            self._set_status("")
            return
        self._set_busy(False)
        if found.state == FOUND:
            if found.under:
                # Out under the file's own name: that is the article, and
                # its link name, exactly as it is (another would be a
                # second article). This computer records it once it went
                # out (_on_completed).
                slug = found.under
                self._slug_is_auto = False
                self._kept_slug = slug
                self._file_names = []                  # decided
                self._slug_edit.setText(slug)
            self._first_published[slug] = found.published_at
            self._publish(slug, self._body, found.published_at)
        elif found.state == NEVER:
            # A new article; one whose date is known (its front matter's)
            # keeps that date.
            self._publish(slug, self._body, known or int(time.time()))
        else:
            self._set_status("")
            self._ask_when_nobody_can_tell(slug, self._body, file_names, known)

    def _ask_when_nobody_can_tell(self, slug: str, body: str,
                                  file_names: Sequence[str] = (),
                                  known: Optional[int] = None) -> None:
        """The relays gave no clear answer: whether the article went out
        before cannot be told, and a guess would either re-date an edit or
        back-date a new article, or for a file's article make a second one
        beside one under the file's own name. The person decides: for a
        file's article between its own name, exactly as it is (typed, it
        would be written in one form, another name for a Mac's file), and
        the made one."""
        if file_names:
            message = _("The relays gave no clear answer. Earlier versions of "
                        "MyEditor published this file under its own name, “{name}”, so the "
                        "article may be out already under that name. Publishing it under "
                        "another name makes a second article.").format(name=file_names[0])
            choices = (Button(_("Publish as “{name}”").format(name=file_names[0]), "file",
                              NORMAL),
                       Button(_("Publish as “{name}”").format(name=slug), "new", NORMAL))
        else:
            message = _("The relays gave no clear answer. If the article went "
                        "out before, publishing it as new gives it today's date, and "
                        "readers see it as a new article.")
            choices = (Button(_("Publish as New"), "new", NORMAL),)
        choice = ask(
            self,
            title=_("Couldn't check whether this article was published before"),
            message=message,
            buttons=(*choices,
                     Button(_("Cancel"), "cancel", CANCEL),
                     Button(_("Try Again"), "again", DEFAULT)),
            is_dark=self._is_dark, nostr=True)
        if choice == "again":
            self._on_publish()
        elif choice == "file":
            slug = file_names[0]
            self._slug_is_auto = False
            self._kept_slug = slug
            self._file_names = []                      # decided
            self._slug_edit.setText(slug)
            self._publish(slug, body, known or int(time.time()))
        elif choice == "new":
            self._publish(slug, body, known or int(time.time()))

    def _publish(self, slug: str, body: str, published_at: int) -> None:
        profile = self._account.profile()
        try:
            unsigned = build_article(
                content=body,
                pubkey_hex=profile.user_pubkey,
                slug=slug,
                title=self._title_edit.text(),
                summary=self._summary_edit.text(),
                image=self._image_edit.text(),
                published_at=published_at,
                hashtags=list(self._hashtags.tags()),
                media=self._media,
                extra_tags=self._details.published_tags(body, author=(
                    profile.user_pubkey, self._relay_directory.relay_hint(profile.user_pubkey)))
                or None,
            )
        except ValueError as exc:
            self._show_details(True)
            self._set_status(_("Cannot build article: {reason}").format(reason=exc), error=True)
            return
        unsigned = publishable(self, unsigned, is_dark=self._is_dark)
        if unsigned is None:
            return

        self._set_busy(True)
        self._set_status("")
        self._publishing = (slug, published_at)

        self._job = PublishJob(
            relay_pool=self._relay_pool,
            relay_directory=self._relay_directory,
            session_pool=self._session_pool,
            profile=profile,
            entitled_relays=relays_from(self._entitled_relays),
            unsigned_event=unsigned,
            parent=self,
        )
        self._job.status_changed.connect(self._set_status)
        self._job.signed.connect(self._on_signed)
        self._job.completed.connect(self._on_completed)
        self._job.failed.connect(self._on_failed)
        self._job.start()

    def _on_signed(self, event_id_hex: str) -> None:
        self._signed_event_id = event_id_hex

    def _on_completed(self, results: List[PublishResult]) -> None:
        self._job = None
        accepted = sum(1 for _url, ok, _message in results if ok)
        if accepted == 0:
            self._set_status(
                _("The article was not published: no relay took it. Check your internet "
                  "connection and try again."), error=True
            )
            self._set_busy(False)
            return
        author = self._account.profile().user_pubkey
        # What it went out as: the document's record of it (details()).
        self._published_as = self._publishing
        slug, published_at = self._published_as
        if self._first_publications is not None and published_at:
            # This computer remembers it, for when the relays cannot tell.
            self._first_publications.remember(author, slug, published_at)
        hint_relays = [url for url, ok, _message in results if ok][:2]
        naddr = encode_naddr(
            identifier=slug,
            author_pubkey_hex=author,
            kind=30023,
            relays=hint_relays,
        )
        self.published.emit(naddr, results)
        self.accept()

    def _on_failed(self, reason: str) -> None:
        self._job = None
        self._set_status(_("Publish failed: {reason}").format(reason=humanize_failure(reason)),
                         error=True)
        self._set_busy(False)

    def _on_cancel(self) -> None:
        self.reject()

    def reject(self) -> None:
        # Cancel, Escape and the window's close button all end here. A
        # signer request already sent can't be revoked, but the job stops:
        # a signature that arrives later is not published.
        if self._job is not None:
            self._job.cancel()
            self._job = None
        self._looking_up = False
        super().reject()
