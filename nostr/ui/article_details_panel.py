# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Article Details: the inspector at the trailing edge of the window, as
Pages keeps a document's details beside it, for the article in the tab in
front.

It shows and edits the one model of the article's details
(nostr/article_details.py, kept with the tab by the window), the same
details the publish window offers: title, summary, cover image, hashtags,
and under Advanced the article's address (its slug) and when it first
went out. What is typed goes to the tab at once; nothing here publishes
or saves.

The address of an article that already has one (a saved draft's, a
published article's) is shown, not edited: another address would be
another article. A tab that holds no article (a note's draft, a PDF)
says so instead of showing empty fields.

It is grouped in sections as Pages groups its inspector (Article, Cover,
Hashtags, Authors, Advanced), each titled, with a line between them. The
cover is one image well (cover_field.py). Co-authors are chosen the way
a mention is, from people whose Nostr address is confirmed; they are
tagged as authors of the article, and each can be given a share of the
zaps to it (authors_section.py).

It exists only while a Nostr account is in use (decision D-1).
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QScrollArea, QStackedWidget,
    QToolButton, QVBoxLayout, QWidget,
)

import announce
import theme
from . import appearance
from i18n import _, format_moment

from ..article_details import ArticleDetails
from ..blossom.store import MediaStore
from ..media.media_visibility import MediaVisibility
from ..media.private_library import PrivateLibrary
from ..media.publish_copy import PublicCopyMaker
from .media_saver import MediaSaver
from ..profiles import Profile
from ..publisher import link_name, slugify
from .authors_section import AuthorsSection
from .coauthors import Coauthors
from .cover_field import CoverImageField
from .secret_key_check import slug_problem
from .tag_field import TagField

MIN_WIDTH = 260
DEFAULT_WIDTH = 300
MAX_WIDTH = 420


class _GrowingSummary(QPlainTextEdit):
    """The summary: two lines to start, growing with what is written up to
    five, as a field sized to what it is meant to hold."""

    LINES = (2, 5)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Tab goes on to the next field, as in a form.
        self.setTabChangesFocus(True)
        self.textChanged.connect(self._fit)
        self._fit()

    def _fit(self) -> None:
        # A plain text layout measures its document in lines.
        lines = int(self.document().documentLayout().documentSize().height())
        least, most = self.LINES
        shown = max(least, min(most, lines))
        # Scrolls only past the most lines it grows to.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if lines > most
                                        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Around the text: the frame and padding (the style's), as they are.
        chrome = max(0, self.height() - self.viewport().height())
        margin = 2 * int(self.document().documentMargin())
        self.setFixedHeight(shown * self.fontMetrics().lineSpacing() + margin + chrome)

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        self._fit()

    def showEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().showEvent(event)
        self._fit()                     # the style's padding is known now


class ArticleDetailsPanel(QFrame):
    """The inspector. ``changed(ArticleDetails)`` as the person edits. It
    is shown and hidden by Article Details (the toolbar's toggle, the
    View and Nostr menus, its key), so it has no close button."""

    changed = Signal(object)
    # (document, address): a picture dropped as the cover of a document no
    # longer in front is uploaded: that document's cover.
    cover_uploaded = Signal(object, str)

    def __init__(self, *, media_store: Optional[MediaStore] = None,
                 media_visibility: Optional[MediaVisibility] = None,
                 copy_maker: Optional[PublicCopyMaker] = None,
                 private_library: Optional[PrivateLibrary] = None,
                 media_saver: Optional[MediaSaver] = None,
                 profile: Callable[[], Optional[Profile]] = lambda: None,
                 pick_person: Optional[Callable[[QWidget, Callable], None]] = None,
                 name_of: Callable[[str], str] = lambda pubkey: pubkey,
                 can_receive_zaps: Optional[Callable[[str, str, Callable], None]] = None,
                 relay_hint: Callable[[str], str] = lambda pubkey: "",
                 is_dark: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Who a co-author is asked of (the window's picker, at a widget,
        # answering with a Person), and how a person is called.
        self._pick_person = pick_person
        self._name_of = name_of
        # Adding and removing co-authors, and sharing zaps with them, as
        # Publish as Article does: whether someone can receive zaps
        # (pubkey, relay hint, answer(bool)) and the relay a zap to someone
        # is announced on decide the shares.
        self._coauthors = Coauthors(profile=profile, relay_hint=relay_hint,
                                    can_receive_zaps=can_receive_zaps)
        self.setObjectName("ArticleDetailsPanel")
        self.setAccessibleName(_("Article Details"))
        self.setMinimumWidth(MIN_WIDTH)
        self.setMaximumWidth(MAX_WIDTH)
        self._is_dark = is_dark
        self._shown = ArticleDetails()
        # The link name of the article shown, kept letter for letter, and
        # the one publishing would make when none is typed (a file's).
        self._kept_identifier = ""
        self._made_identifier = ""
        # The title the article publishes with while none is typed.
        self._derived_title = ""
        self._filling = False
        # The details the file writes in a way that is kept as written
        # (article_details.kept_in_file), and the line under each.
        self._kept: frozenset = frozenset()
        self._kept_notes: dict = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        self._pages = QStackedWidget()
        outer.addWidget(self._pages, 1)

        self._form = self._build_form(media_store, media_visibility, copy_maker,
                                      private_library, profile, media_saver)
        self._pages.addWidget(self._form)
        self._no_article = QLabel()
        self._no_article.setObjectName("article_details_note")
        self._no_article.setWordWrap(True)
        self._no_article.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._no_article.setContentsMargins(16, 16, 16, 16)
        self._pages.addWidget(self._no_article)
        self._apply_theme()

    # -- building -------------------------------------------------------------

    def _build_header(self) -> QWidget:
        """The toolbar band's section over this pane: the band's background
        and line, as tall as the format toolbar (``align_band``)."""
        header = QFrame()
        header.setObjectName("article_details_header")
        self._header = header
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 6, 8, 6)
        title = QLabel(_("Article Details"))
        title.setObjectName("article_details_title")
        row.addWidget(title, 1)
        return header

    def _build_form(self, media_store, media_visibility, copy_maker, private_library,
                    profile, media_saver=None) -> QWidget:
        scroll = QScrollArea()
        scroll.setObjectName("article_details_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        body = QWidget()
        body.setObjectName("article_details_body")
        form = QVBoxLayout(body)
        form.setContentsMargins(16, 4, 16, 16)
        form.setSpacing(6)

        # When the file keeps every detail as written: said once, here.
        self._all_kept_note = QLabel(_("Every detail is kept as written in the file: "
                                       "MyEditor can’t read its front matter in full."))
        self._all_kept_note.setObjectName("article_details_hint")
        self._all_kept_note.setWordWrap(True)
        self._all_kept_note.hide()
        form.addWidget(self._all_kept_note)

        # Grouped as Pages groups its inspector: a title over each section and
        # a line between them, what is changed most often first.
        self._section(form, _("Article"), first=True)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText(_("Article title"))
        self._add_labeled(form, _("Title"), self.title_edit)
        self._add_kept_note(form, "title")

        self.summary_edit = _GrowingSummary()
        self.summary_edit.setPlaceholderText(_("Shown under the title in feeds"))
        self._add_labeled(form, _("Summary"), self.summary_edit)
        self._add_kept_note(form, "summary")

        self._section(form, _("Cover"))
        self.cover = CoverImageField(
            media_store=media_store, media_visibility=media_visibility,
            copy_maker=copy_maker, private_library=private_library, profile=profile,
            saver=media_saver, is_dark=self._is_dark, stacked=True)
        self.cover.problem.connect(self._say_cover_problem)
        self.cover.landed_for.connect(self.cover_uploaded)
        form.addWidget(self.cover)
        self.cover_problem = self._problem_label()
        form.addWidget(self.cover_problem)
        self._add_kept_note(form, "image")

        self._section(form, _("Hashtags"))
        self.hashtags = TagField(is_dark=self._is_dark)
        self.hashtags.setAccessibleName(_("Hashtags"))
        form.addWidget(self.hashtags)
        self._add_kept_note(form, "tags")

        self._section(form, _("Authors"))
        self.authors = AuthorsSection(
            coauthors=self._coauthors, name_of=self._name_of, pick_person=self._pick_person,
            own_pubkey=lambda: getattr(profile(), "user_pubkey", ""),
            own_name=lambda: getattr(profile(), "display_name", ""), parent=body)
        self.authors.changed.connect(self._hand_back)
        form.addWidget(self.authors)
        self._add_kept_note(form, "coauthors")

        self._section(form, "")
        # Advanced: what most people never change, closed at first.
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setObjectName("article_details_disclosure")
        self.advanced_toggle.setText(_("Advanced"))
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.advanced_toggle.toggled.connect(self._show_advanced)
        form.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        advanced.setSpacing(6)
        self.identifier_edit = QLineEdit()
        self._add_labeled(advanced, _("Link name"), self.identifier_edit)
        self._add_kept_note(advanced, "slug")
        self.identifier_note = QLabel()
        self.identifier_note.setObjectName("article_details_hint")
        self.identifier_note.setWordWrap(True)
        advanced.addWidget(self.identifier_note)
        self.identifier_problem = self._problem_label()
        advanced.addWidget(self.identifier_problem)
        self.first_published = QLabel()
        self.first_published.setObjectName("article_details_hint")
        self.first_published.setWordWrap(True)
        advanced.addWidget(self.first_published)
        self.advanced.setVisible(False)
        form.addWidget(self.advanced)
        form.addStretch(1)
        scroll.setWidget(body)

        for edit in (self.title_edit, self.identifier_edit):
            edit.textChanged.connect(self._edited)
        self.hashtags.changed.connect(self._edited)
        self.summary_edit.textChanged.connect(self._edited)
        self.cover.changed.connect(self._edited)
        return scroll

    def _section(self, layout, title: str, *, first: bool = False) -> None:
        """A section's start: a line above it (but the first) and its title."""
        if not first:
            layout.addSpacing(8)
            line = QFrame()
            line.setObjectName("article_details_section_line")
            line.setFixedHeight(1)
            layout.addWidget(line)
            layout.addSpacing(4)
        if title:
            heading = QLabel(title)
            heading.setObjectName("article_details_section")
            heading.setAccessibleName(title)
            layout.addWidget(heading)

    @staticmethod
    def _add_labeled(layout, text: str, field) -> None:
        label = QLabel(text)
        label.setObjectName("article_details_label")
        label.setBuddy(field)
        field.setAccessibleName(text)
        layout.addSpacing(6)
        layout.addWidget(label)
        layout.addWidget(field)

    def _add_kept_note(self, layout, detail: str) -> None:
        """The plain line under a detail the file keeps as written."""
        note = QLabel(_("Kept as written in the file."))
        note.setObjectName("article_details_hint")
        note.setWordWrap(True)
        note.hide()
        layout.addWidget(note)
        self._kept_notes[detail] = note

    @staticmethod
    def _problem_label() -> QLabel:
        label = QLabel()
        label.setObjectName("article_details_problem")
        label.setWordWrap(True)
        label.hide()
        return label

    def _apply_theme(self) -> None:
        dark = self._is_dark
        r = appearance.roles(dark)
        background, border, field, text, muted = r.panel, r.edge, r.field, r.text, r.muted
        band = r.chrome
        self.setStyleSheet(
            appearance.dialog_stylesheet(dark)
            # No edge of its own: the window's divider is its left edge.
            + f"#ArticleDetailsPanel {{ background: {background}; }}"
            + f"#article_details_header {{ background: {band};"
              f" border: none; border-bottom: 1px solid {border}; }}"
            + "#article_details_title { font-weight: 600; }"
            # Each section's title over it, a hairline between sections.
            + f"#article_details_section {{ font-weight: 600; color: {text}; }}"
            + f"QFrame#article_details_section_line {{ background: {border}; border: none; }}"
            # Authors: a list as a Mac's settings draw one, + and − under it.
            + f"QTableWidget#article_details_authors {{ background: {field}; color: {text};"
              f" border: 1px solid {border}; border-radius: 5px; outline: none; }}"
            + "QTableWidget#article_details_authors::item { padding: 0 6px; border: none; }"
            + f"QTableWidget#article_details_authors::item:selected {{"
              f" background: {r.selection}; color: white; }}"
            + f"QToolButton#article_details_list_button {{ background: {field}; color: {text};"
              f" border: 1px solid {border}; border-radius: 4px; font-size: 14px; }}"
            + f"QToolButton#article_details_list_button:disabled {{ color: {muted}; }}"
            + f"QPushButton#article_details_link {{ border: none; background: transparent;"
              f" color: {r.ink}; padding: 0 2px; }}"
            + f"#article_details_scroll, #article_details_body {{ background: {background}; }}"
            + f"QLineEdit, QPlainTextEdit {{ background: {field}; color: {text};"
              f" border: 1px solid {border}; border-radius: 4px; padding: 3px 6px; }}"
            # Shown, not edited: on the panel's own ground, in muted ink.
            + f"QLineEdit:read-only, QPlainTextEdit[readOnly=\"true\"] {{"
              f" background: {background}; color: {muted}; }}"
            + f"#article_details_label, #article_field_label {{ color: {muted}; }}"
            + f"#article_details_hint, #article_field_hint, #article_details_note"
              f" {{ color: {muted}; }}"
            + f"#article_details_problem {{ color: {theme.error_text_color(dark)}; }}"
            + f"QLabel#article_cover_preview {{ border: 1px solid {border};"
              f" border-radius: 4px; color: {muted}; }}"
            + "QToolButton#article_details_disclosure"
              " { border: none; padding: 2px 4px; }")
        appearance.surface(self, dark)

    def align_band(self, toolbar_height: int) -> None:
        """The header as tall as the format toolbar beside it, so their
        lines meet (0: its own height)."""
        self._header.setMinimumHeight(max(0, int(toolbar_height)))

    def set_dark(self, is_dark: bool) -> None:
        appearance.bind(self, "set_dark")
        self._is_dark = is_dark
        self.cover.set_dark(is_dark)
        self.hashtags.set_dark(is_dark)
        self._apply_theme()

    # -- showing a tab's article ------------------------------------------------

    def show_article(self, details: ArticleDetails, *, fixed_identifier: bool,
                     kept_in_file: frozenset = frozenset(), document: object = None,
                     made_identifier: str = "") -> None:
        """Show ``details``, the article in the tab in front, without
        handing them back. ``fixed_identifier``: the article has its
        address already (a saved draft, a published article).
        ``kept_in_file``: the details its file writes in a way that stays as
        written (article_details.kept_in_file), shown read-only.
        ``document``: whose article it is, which a cover dropped for it
        lands on (cover_uploaded) once another is in front.
        ``made_identifier``: the link name Publish as Article starts with
        when none is typed (a file's article: made of the file's name), as
        the field's placeholder; without it, the one made of the title."""
        self._made_identifier = made_identifier
        # What the fields hold already (the person's own typing, handed
        # back and shown again) is left as typed: a space at the end of a
        # word being written stays.
        typed = self._pages.currentWidget() is self._form and details == self.details()
        self._filling = True
        try:
            # Another document's cover is shown as it is, and a picture
            # uploading for the one shown before goes on for that one.
            self.cover.set_owner(document, details.image)
            self._shown = details
            self._show_kept(kept_in_file)
            if not typed:
                self._fill_fields(details)
            else:
                self._show_people(details)
            self.identifier_edit.setReadOnly(fixed_identifier or "slug" in self._kept)
            self.identifier_note.setText(
                _("A saved or published article keeps its link name; saving it under "
                  "another one makes another article.") if fixed_identifier else "")
            self.identifier_note.setVisible(fixed_identifier)
            self._show_first_published(details.published_at)
            self._check_identifier()
            self._suggest_identifier()
        finally:
            self._filling = False
        self._pages.setCurrentWidget(self._form)

    def _show_kept(self, kept: frozenset) -> None:
        """The details kept as written in the file: read-only, saying so."""
        self._kept = frozenset(kept)
        every = set(self._kept_notes) | {"date", "zap_split"} <= self._kept
        self._all_kept_note.setVisible(every)
        for detail, note in self._kept_notes.items():
            # One line for the co-authors and how zaps are shared.
            note.setVisible(not every and (detail in self._kept or detail == "coauthors"
                                           and "zap_split" in self._kept))
        self.title_edit.setReadOnly("title" in self._kept)
        self.summary_edit.setReadOnly("summary" in self._kept)
        self.cover.setReadOnly("image" in self._kept)
        self.hashtags.setReadOnly("tags" in self._kept)
        # The read-only look comes from a property the style sheet reads.
        self.summary_edit.style().unpolish(self.summary_edit)
        self.summary_edit.style().polish(self.summary_edit)

    def _fill_fields(self, details: ArticleDetails) -> None:
        self._set_text(self.title_edit, details.title)
        if self.summary_edit.toPlainText() != details.summary:
            self.summary_edit.setPlainText(details.summary)
        self._set_text(self.cover.url_edit, details.image)
        if self.hashtags.tags() != details.hashtags:
            self.hashtags.set_tags(details.hashtags)
        self._set_text(self.identifier_edit, details.identifier)
        self._kept_identifier = details.identifier
        self._show_people(details)

    def shows_article(self) -> bool:
        """Whether the fields are shown (not why there are none)."""
        return self._pages.currentWidget() is self._form

    def show_no_article(self, reason: str) -> None:
        """The tab in front holds no article: ``reason`` instead of fields."""
        self.cover.set_owner(None)
        self._no_article.setText(reason)
        self._pages.setCurrentWidget(self._no_article)

    @staticmethod
    def _set_text(edit: QLineEdit, text: str) -> None:
        if edit.text() != text:
            edit.setText(text)
            if edit.isReadOnly():
                edit.setCursorPosition(0)       # shown from its start

    def set_derived_title(self, title: str) -> None:
        """The title the article publishes with while none is typed (its
        first heading or line, article_details.default_title), shown as
        the Title's placeholder; the link name's placeholder follows it."""
        self._derived_title = title
        self.title_edit.setPlaceholderText(title or _("Article title"))
        self._suggest_identifier()

    def _show_first_published(self, when: Optional[int]) -> None:
        if when:
            moment = QDateTime.fromSecsSinceEpoch(int(when))
            self.first_published.setText(
                _("First published {moment}").format(moment=format_moment(moment)))
        else:
            self.first_published.setText(_("Not published yet."))

    # -- co-authors and their shares ---------------------------------------------

    def _show_people(self, details: ArticleDetails) -> None:
        """The authors and how the zaps are shared (authors_section.py)."""
        self.authors.show_details(details, editable=not ({"coauthors", "zap_split"}
                                                         & self._kept))

    def _hand_back(self, details: ArticleDetails) -> None:
        self._shown = details
        self.changed.emit(details)

    # -- editing ----------------------------------------------------------------

    def details(self) -> ArticleDetails:
        """What the fields hold, on the details shown."""
        return self._shown.with_changes(
            title=self.title_edit.text().strip(),
            summary=self.summary_edit.toPlainText().strip(),
            image=self.cover.text().strip(),
            hashtags=self.hashtags.tags(),
            identifier=self._identifier())

    def _identifier(self) -> str:
        """The link name in its field, as it is published: the article's
        own exactly as it is, one typed in one form (link_name)."""
        return link_name(self.identifier_edit.text(), self._kept_identifier)

    def _edited(self, *_args) -> None:
        self._check_identifier()
        self._suggest_identifier()
        if self._filling:
            return
        self.cover_problem.hide()
        details = self.details()
        if details != self._shown:
            self._shown = details
            self.changed.emit(details)

    def _check_identifier(self) -> None:
        announce.field_problem(self.identifier_edit, self.identifier_problem,
                               slug_problem(self._identifier()) or "")
        if not self.identifier_problem.isHidden():
            self.advanced_toggle.setChecked(True)

    def _suggest_identifier(self) -> None:
        """An empty address shows the one Publish as Article would start
        with, as a placeholder: a file's article the one made of the file's
        name; any other the one made of its title, following it."""
        title = self.title_edit.text().strip() or self._derived_title
        self.identifier_edit.setPlaceholderText(
            self._made_identifier or (slugify(title) if title else _("my-article")))

    def _say_cover_problem(self, reason: str) -> None:
        announce.field_problem(self.cover.url_edit, self.cover_problem, reason)

    def _show_advanced(self, shown: bool) -> None:
        self.advanced.setVisible(shown)
        self.advanced_toggle.setArrowType(
            Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
