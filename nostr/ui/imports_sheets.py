# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Imports window's sheets: Follow a Website, Import a File, Import a
Link.

Each asks one thing and shows what it found before anything is kept, as
EINUNDZWANZIG STANDUP's FollowSourceSheet, ImportFileSheet and
ImportLinkSheet do:

Follow a Website
    Continue looks the address up, and a card shows the source: its
    name, what it is (Feed, Nostr author, Collection), how many posts it
    has and how new the newest is. Follow keeps it. A source followed
    already offers Show Source; a single post offers Import Once (it
    opens like a link and is never kept as a source); a list of sources
    (an address ending in .opml, or pasted) offers Follow All. When
    nothing could be read, the error says why, and Paste the Feed
    Instead takes the feed's own text for a website that turns readers
    away.

Import a File
    Choose File... or drop a file on the sheet or on the window: a blog
    export, a feed or a Markdown file opens in the sidebar with all its
    posts checked; a list of sources offers Follow All.

Import a Link
    A post, a thread, a Nostr article or a Markdown file opens in the
    sidebar to be imported once. An address that is a whole source says
    so and offers Follow.

Each is a sheet on the Imports window (window-modal: on macOS it slides
down from the window's title bar) with one page whose content and
default button follow what was found. The app's assistant lays out the
buttons: the likely action at the trailing edge, where Return lands,
Cancel beside it. Nothing is stored before that button is pressed. A
sheet asks the imports controller and says what was decided through its
signals; it keeps no import state of its own.
"""

from __future__ import annotations

import os
from typing import List, Optional
from urllib.parse import urlsplit

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import file_paths
from alerts import answer_return_as_the_system_does
from i18n import _, ngettext

from ..imports.checker import checks_automatically
from ..imports.feed_list import source_key
from ..imports.intake import (
    FEED,
    FOLLOWABLE,
    is_list_address,
    is_whole_source,
    kind_of,
    kind_word,
    looks_like_markup,
    opml_titles,
)
from ..imports.registry import can_resolve_source
from ..rss.discovery import normalize_user_url
from ..imports.sources.opml import OpmlDocument, is_opml, parse_opml
from ..imports.workspace import date_text
from ..imports_controller import NO_SOURCES_IN_LIST, NOTHING_FOUND
from .assistant import (
    DEFAULT,
    LEADING,
    NORMAL,
    AssistantWindow,
    busy_bar,
    link_button,
    page,
    text_label,
)
from .imports_activity import with_its_word
from .imports_glyphs import letter_avatar

MAIN = "main"

# What the Follow sheet shows.
IDLE, LOOKING, FOUND, FOLLOWED, SINGLE_POST, SOURCES = (
    "idle", "looking", "found", "followed", "single", "sources")

INVALID_ADDRESS = _("That doesn't look like the address of a website, a feed or a Nostr "
                    "author.")
NEEDS_ADDRESS = _("Add the feed's address above as well, so the links in it work.")
CANT_FOLLOW = _("This address can't be followed.")

# The Follow sheet's "What Works Here".
FOLLOW_EXAMPLES = (
    _("A website or a blog: example.com (MyEditor finds its feed)"),
    _("WordPress: example.com/feed/"),
    _("Substack: name.substack.com/feed"),
    _("Ghost: example.com/rss/"),
    _("Medium: medium.com/feed/@name"),
    _("Hugo: example.com/index.xml"),
    _("A podcast's or a YouTube channel's feed"),
    _("A Nostr author: name@example.com, or a link to their profile"),
    _("A sitemap: example.com/sitemap.xml"),
    _("A GitHub folder of Markdown files"),
    _("A list of sources: an address ending in .opml"),
)

# Where each platform keeps its export, for the File sheet.
EXPORT_GUIDES = (
    _("WordPress: Tools > Export, then choose the .xml file here."),
    _("Ghost: Settings > Import/Export > Export, then choose the .json file here."),
    _("Medium: Settings > Download your information. The .zip comes by email."),
    _("Substack: Settings > Exports > New export, then choose the .zip file here."),
)

FILE_FILTER = _("Exports and feeds (*.zip *.xml *.json *.opml *.md *.mdx *.markdown *.rss "
                "*.atom);;All files (*)")


def found_summary(result) -> str:
    """"12 posts found, newest 2 h ago"."""
    items = list(result.feed.items)
    count = len(items)
    newest = max((item.published_at or 0 for item in items), default=0)
    if newest:
        return ngettext("{count} post found, from {when}", "{count} posts found, newest {when}",
                        count).format(count=count, when=date_text(newest, in_sentence=True))
    return ngettext("{count} post found", "{count} posts found", count).format(count=count)


def list_lines(document: OpmlDocument) -> str:
    """The first titles of a list of sources, and how many more."""
    titles, more = opml_titles(document)
    lines = list(titles)
    if more:
        lines.append(ngettext("and {count} more", "and {count} more", more).format(count=more))
    return "\n".join(lines)


def follow_all_label(count: int) -> str:
    return ngettext("Follow It", "Follow All {count}", count).format(count=count)


def list_text(count: int) -> str:
    return ngettext("This list holds {count} source.", "This list holds {count} sources.",
                    count).format(count=count)


class _Card(QFrame):
    """What was found: an initial, the name, what it is and its posts."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("imports_card")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(32, 32)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = text_label("", "item_title")
        self.detail = text_label("", "muted")
        text.addWidget(self.title)
        text.addWidget(self.detail)
        row.addLayout(text, 1)

    def show_result(self, result, kind: str, fallback: str) -> None:
        title = result.feed.title or result.url or fallback
        self.title.setText(title)
        self.detail.setText("  ·  ".join((kind_word(kind), found_summary(result))))
        self.icon.setPixmap(letter_avatar(title, result.url or title, 32,
                                          dpr=self.devicePixelRatioF()))
        self.setAccessibleName(f"{title}, {self.detail.text()}")


class _Sheet(AssistantWindow):
    """What every Imports sheet shares: one page, a wait, an error, and a
    default button that follows the state."""

    def __init__(self, controller, title: str, *, dark: bool, parent=None) -> None:
        super().__init__(title, is_dark=dark, min_width=500, parent=parent)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.apply_theme(dark)
        self._controller = controller
        self._generation = 0
        self._shown_buttons: List[tuple] = []
        widget, self.column = page(10)
        self.heading = text_label(title, "title")
        self.column.addWidget(self.heading)
        self.add_page(MAIN, widget)

    def apply_theme(self, dark: bool) -> None:
        from .appearance import roles
        super().apply_theme(dark)
        border = roles(dark).edge
        self.setStyleSheet(self.styleSheet() + f"""
            QFrame#imports_card, QFrame#imports_drop {{
                border: 1px solid {border}; border-radius: 8px;
            }}
            QFrame#imports_drop {{ border-style: dashed; }}
            QFrame#imports_drop[active="true"] {{ border-style: solid; border-width: 2px; }}
        """)

    # -- parts ---------------------------------------------------------------------

    def _add_busy(self) -> None:
        self.busy = QWidget()
        row = QHBoxLayout(self.busy)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.busy_label = text_label("", "muted")
        row.addWidget(busy_bar(120))
        row.addWidget(self.busy_label, 1)
        self.busy.hide()
        self.column.addWidget(self.busy)

    def _add_error(self) -> None:
        self.error = text_label("", "error")
        self.error.hide()
        self.column.addWidget(self.error)

    def _finish(self) -> None:
        self.column.addStretch(1)
        self.show_page(MAIN, [])
        self._update_buttons()

    # -- state ---------------------------------------------------------------------

    def _wait(self, text: str) -> int:
        """Show the wait; returns the number to check the answer against."""
        self._generation += 1
        self.busy_label.setText(with_its_word(text))
        self.busy.show()
        self.error.hide()
        self._update_buttons()
        return self._generation

    def _waiting(self) -> bool:
        return not self.busy.isHidden()

    def _current(self, generation: int) -> bool:
        """Whether an answer still matters: nothing was asked since and
        the sheet was not closed."""
        if generation != self._generation:
            return False
        self.busy.hide()
        return True

    def _fail(self, text: str) -> None:
        self._generation += 1
        self.busy.hide()
        self.error.setText(text)
        self.error.show()
        self._update_buttons()

    def _buttons(self) -> list:
        raise NotImplementedError

    def _update_buttons(self) -> None:
        """Lay the buttons out again only when they change, and keep their
        enabled state current."""
        specs = self._buttons()
        shape = [(key, label, placement) for key, label, placement, _handler, _on in specs]
        if shape != self._shown_buttons:
            self._shown_buttons = shape
            self.set_buttons([spec[:4] for spec in specs])
        for key, _label, _placement, _handler, enabled in specs:
            button = self.buttons.get(key)
            if button is not None:
                button.setEnabled(enabled)
        self.fit_page()

    def done(self, result: int) -> None:
        self._generation += 1   # an answer still on its way is dropped
        super().done(result)


class FollowSheet(_Sheet):
    """Follow a Website.

    Signals:
      followed(str)              a source was followed (its key)
      followed_list(int, int, int)  a list's sources: followed now, followed
                                 before, could not be followed
      show_source(str)           the source is followed already (its key)
      import_once(object)        a single post, to open once (the lookup's result)
    """

    followed = Signal(str)
    followed_list = Signal(int, int, int)
    show_source = Signal(str)
    import_once = Signal(object)

    def __init__(self, controller, *, dark: bool = False, address: str = "",
                 parent=None) -> None:
        super().__init__(controller, _("Follow a Website"), dark=dark, parent=parent)
        self._state = IDLE
        self._result = None
        self._document: Optional[OpmlDocument] = None
        self._followed_key = ""
        self._paste_mode = False

        label = text_label(_("Website, feed or Nostr address"))
        self.address = QLineEdit(address)
        self.address.setObjectName("imports_address")
        self.address.setPlaceholderText(_("example.com  ·  example.com/feed  ·  "
                                          "name@example.com"))
        self.address.setClearButtonEnabled(True)
        self.address.setAccessibleName(_("Website, feed or Nostr address"))
        label.setBuddy(self.address)
        self.address.textChanged.connect(self._edited)
        self.column.addWidget(label)
        self.column.addWidget(self.address)
        self.column.addWidget(text_label(_("New posts wait in Imports. Nothing is "
                                           "published."), "help"))

        self.examples_button = link_button(_("Show What Works Here"), self._toggle_examples)
        self.column.addWidget(self.examples_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.examples = text_label("\n".join("• " + line for line in FOLLOW_EXAMPLES),
                                   "help")
        self.examples.hide()
        self.column.addWidget(self.examples)

        self.paste_box = QWidget()
        paste = QVBoxLayout(self.paste_box)
        paste.setContentsMargins(0, 4, 0, 0)
        paste.setSpacing(6)
        paste_label = text_label(_("Feed XML or JSON"))
        self.paste = QPlainTextEdit()
        self.paste.setObjectName("imports_paste")
        self.paste.setAccessibleName(_("Feed XML or JSON"))
        self.paste.setTabChangesFocus(True)
        self.paste.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.paste.setMinimumHeight(110)
        self.paste.textChanged.connect(self._edited)
        paste_label.setBuddy(self.paste)
        paste.addWidget(paste_label)
        paste.addWidget(self.paste)
        paste.addWidget(text_label(_("Open the feed's address in your browser, show the "
                                     "page source, copy all of it and paste it here. For "
                                     "an XML feed, the address above is needed as well."),
                                   "help"))
        paste.addWidget(link_button(_("Use an Address Instead"), self.use_address),
                        0, Qt.AlignmentFlag.AlignLeft)
        self.paste_box.hide()
        self.column.addWidget(self.paste_box)

        self._add_busy()
        self.card = _Card()
        self.card.hide()
        self.column.addWidget(self.card)
        self.note = text_label("")
        self.note.hide()
        self.column.addWidget(self.note)
        self.sources = text_label("", "muted")
        self.sources.hide()
        self.column.addWidget(self.sources)
        self._add_error()
        self.paste_button = link_button(_("Paste the Feed Instead"), self.use_paste)
        self.paste_button.hide()
        self.column.addWidget(self.paste_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._finish()
        self.address.setFocus()

    @property
    def state(self) -> str:
        return LOOKING if self._waiting() else self._state

    # -- the buttons -----------------------------------------------------------------

    def _buttons(self) -> list:
        cancel = ("cancel", _("Cancel"), NORMAL, self.reject, True)
        state = self._state
        if state == FOUND:
            primary = ("follow", _("Follow"), DEFAULT, self._follow, True)
        elif state == FOLLOWED:
            primary = ("show", _("Show Source"), DEFAULT, self._show_followed, True)
        elif state == SINGLE_POST:
            primary = ("import", _("Import Once"), DEFAULT, self._import_once, True)
        elif state == SOURCES:
            count = len(self._document.feeds) if self._document else 0
            primary = ("follow_all", follow_all_label(count), DEFAULT, self._follow_all, True)
        else:
            primary = ("continue", _("Continue"), DEFAULT, self.look_up, self._can_look_up())
        return [cancel, primary]

    def _can_look_up(self) -> bool:
        if self._waiting():
            return False
        if self._paste_mode:
            return bool(self.paste.toPlainText().strip())
        return bool(self.address.text().strip())

    # -- what was typed ------------------------------------------------------------

    def _edited(self, *_args) -> None:
        """A changed address or paste: what was found no longer applies."""
        if self._waiting():
            self._generation += 1
            self.busy.hide()
        self._reset()

    def _reset(self) -> None:
        self._state, self._result, self._document = IDLE, None, None
        for widget in (self.card, self.note, self.sources, self.error, self.paste_button):
            widget.hide()
        self._update_buttons()

    def _toggle_examples(self) -> None:
        shown = self.examples.isHidden()
        self.examples.setVisible(shown)
        self.examples_button.setText(_("Hide What Works Here") if shown
                                     else _("Show What Works Here"))
        self.fit_page()

    def use_paste(self) -> None:
        """Paste the Feed Instead: take the feed's own text."""
        self._paste_mode = True
        self.paste_box.show()
        self._reset()
        self.paste.setFocus()

    def use_address(self) -> None:
        self._paste_mode = False
        self.paste_box.hide()
        self._reset()
        self.address.setFocus()

    # -- looking it up -------------------------------------------------------------

    def look_up(self) -> None:
        if not self._can_look_up():
            return
        address = self.address.text().strip()
        if self._paste_mode:
            text = self.paste.toPlainText()
            if is_opml(text):
                self._show_list(parse_opml(text))
                return
            if looks_like_markup(text) and not address:
                self._fail(NEEDS_ADDRESS)
                return
            generation = self._wait(_("Reading the pasted feed…"))
            self._controller.look_up_paste(
                text, address, on_done=lambda result, error: self._answered(
                    generation, result, error))
            return
        if is_list_address(address):
            generation = self._wait(_("Looking at {address}…").format(address=address))
            self._controller.read_list(address, on_done=lambda document, error: (
                self._list_read(generation, document, error)))
            return
        if not can_resolve_source(address):
            self._fail(INVALID_ADDRESS)
            return
        generation = self._wait(_("Looking at {address}…").format(address=address))
        self._controller.look_up(address, on_done=lambda result, error: self._answered(
            generation, result, error))

    def _list_read(self, generation: int, document, error: str) -> None:
        if not self._current(generation):
            return
        if document is None:
            self._fail(error or NO_SOURCES_IN_LIST)
        else:
            self._show_list(document)

    def _answered(self, generation: int, result, error: str) -> None:
        if not self._current(generation):
            return
        if result is None:
            self._fail(error or NOTHING_FOUND)
            self.paste_button.setVisible(not self._paste_mode)
            self.fit_page()
            return
        if not result.url:
            # A pasted feed with no address could never be checked again.
            self._fail(NEEDS_ADDRESS)
            return
        self._found(result)

    def _found(self, result) -> None:
        self._result = result
        typed = self.address.text().strip()
        kind = FEED if self._paste_mode else (kind_of(typed, result.feed.format) or FEED)
        self.card.show_result(result, kind, typed)
        self.card.show()
        followed = next((url for url in (result.url, typed)
                         if url and self._controller.is_followed(url)), "")
        if followed:
            self._state = FOLLOWED
            self._followed_key = source_key(followed)
            self.note.setText(_("You already follow this source."))
        elif kind not in FOLLOWABLE:
            self._state = SINGLE_POST
            self.note.setText(_("This is a single post. It is imported once and not kept "
                                "as a source."))
        else:
            self._state = FOUND
            self.note.setText(_("MyEditor checks this feed for new posts while it's open.")
                              if checks_automatically(result.url) else
                              _("MyEditor reads this source when you open it."))
        self.note.show()
        self._update_buttons()

    def _show_list(self, document: OpmlDocument) -> None:
        if not document.feeds:
            self._fail(NO_SOURCES_IN_LIST)
            return
        self._document = document
        self._state = SOURCES
        self.note.setText(list_text(len(document.feeds)))
        self.note.show()
        self.sources.setText(list_lines(document))
        self.sources.show()
        self._update_buttons()

    # -- acting ------------------------------------------------------------------------

    def _follow(self) -> None:
        result = self._result
        if result is None:
            return
        key = self._controller.follow(result.url, result.feed.title or "", result=result)
        if not key:
            # Q-6's reason when it is that (CP2 review L-e: a pasted feed
            # with a local address said only that it cannot be followed).
            self._fail(self._controller.why_not_followed(result.url) or CANT_FOLLOW)
            return
        self.followed.emit(key)
        self.accept()

    def _show_followed(self) -> None:
        self.show_source.emit(self._followed_key)
        self.accept()

    def _import_once(self) -> None:
        self.import_once.emit(self._result)
        self.accept()

    def _follow_all(self) -> None:
        if self._document is None:
            return
        self.followed_list.emit(*self._controller.follow_all(self._document))
        self.accept()


def dropped_file(mime) -> str:
    """The first local file in a drop, in the app's one spelling (a
    URL's path has forward slashes, also on Windows), or ""."""
    if mime is None or not mime.hasUrls():
        return ""
    for url in mime.urls():
        if url.isLocalFile() and os.path.isfile(url.toLocalFile()):
            return file_paths.normalize(url.toLocalFile())
    return ""


class _DropArea(QFrame):
    """The File sheet's drop target."""

    dropped = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("imports_drop")
        self.setAcceptDrops(True)
        self.setMinimumHeight(84)
        column = QVBoxLayout(self)
        self.label = text_label(_("Drop a file here"), "muted")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(self.label)

    def _set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if dropped_file(event.mimeData()):
            event.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, event) -> None:
        self._set_active(False)

    def dropEvent(self, event) -> None:
        self._set_active(False)
        path = dropped_file(event.mimeData())
        if path:
            event.acceptProposedAction()
            self.dropped.emit(path)


class FileSheet(_Sheet):
    """Import a File.

    Signals:
      opened(str)                the file's posts are in the sidebar (its id)
      followed_list(int, int, int)  a list's sources: followed now, followed
                                 before, could not be followed
    """

    opened = Signal(str)
    followed_list = Signal(int, int, int)

    def __init__(self, controller, *, dark: bool = False, parent=None) -> None:
        super().__init__(controller, _("Import a File"), dark=dark, parent=parent)
        self._document: Optional[OpmlDocument] = None
        self._dialog = None
        # The file whose blog address is asked for (a Ghost export that
        # writes it as a placeholder), while it is.
        self._site_path = ""
        self.column.addWidget(text_label(_("A blog export, a feed or a Markdown file. Its "
                                           "posts open in Imports, and you choose which "
                                           "become drafts.")))
        self.column.addWidget(text_label(_("ZIP from Medium or Substack, WordPress XML, Ghost "
                                           "JSON, RSS, Atom or JSON Feed, Markdown, or a "
                                           "list of sources (OPML)."), "help"))
        self.drop = _DropArea()
        self.drop.dropped.connect(self.read)
        self.column.addWidget(self.drop)
        self.guides_button = link_button(_("Show Where to Find Your Export"),
                                         self._toggle_guides)
        self.column.addWidget(self.guides_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.guides = text_label("\n".join("• " + line for line in EXPORT_GUIDES),
                                 "help")
        self.guides.hide()
        self.column.addWidget(self.guides)
        self._add_busy()
        self.note = text_label("")
        self.note.hide()
        self.column.addWidget(self.note)
        self.sources = text_label("", "muted")
        self.sources.hide()
        self.column.addWidget(self.sources)
        self._add_site()
        self._add_error()
        self._finish()

    def _add_site(self) -> None:
        self.site = QWidget()
        column = QVBoxLayout(self.site)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(text_label(_("This Ghost export doesn't say where the blog lives. "
                                      "With its address, the posts keep their pictures "
                                      "and links.")))
        label = text_label(_("Blog address"))
        self.site_address = QLineEdit()
        self.site_address.setObjectName("imports_site")
        self.site_address.setPlaceholderText(_("yourblog.com"))
        self.site_address.setClearButtonEnabled(True)
        self.site_address.setAccessibleName(_("Blog address"))
        label.setBuddy(self.site_address)
        self.site_address.textChanged.connect(lambda _text: self._update_buttons())
        self.site_address.returnPressed.connect(self._continue_with_site)
        column.addWidget(label)
        column.addWidget(self.site_address)
        self.site.hide()
        self.column.addWidget(self.site)

    def _buttons(self) -> list:
        cancel = ("cancel", _("Cancel"), NORMAL, self.reject, True)
        if self._site_path:
            return [("without", _("Continue Without Address"), LEADING,
                     lambda: self._read_with_site(""), not self._waiting()),
                    cancel,
                    ("continue", _("Continue"), DEFAULT, self._continue_with_site,
                     bool(self._site()) and not self._waiting())]
        if self._document is not None:
            return [("choose", _("Choose Another File…"), LEADING, self.choose,
                     not self._waiting()),
                    cancel,
                    ("follow_all", follow_all_label(len(self._document.feeds)), DEFAULT,
                     self._follow_all, True)]
        return [cancel, ("choose", _("Choose File…"), DEFAULT, self.choose,
                         not self._waiting())]

    def _toggle_guides(self) -> None:
        shown = self.guides.isHidden()
        self.guides.setVisible(shown)
        self.guides_button.setText(_("Hide Where to Find Your Export") if shown
                                   else _("Show Where to Find Your Export"))
        self.fit_page()

    def choose(self) -> None:
        """Choose File...: the system's open panel, as a sheet of this one."""
        dialog = QFileDialog(self, _("Import a File"), "", FILE_FILTER)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.fileSelected.connect(self.read)
        self._dialog = dialog
        dialog.open()

    def read(self, path: str) -> None:
        if not path or self._waiting():
            return
        # Dropped or chosen in the open panel (which answers with forward
        # slashes, also on Windows): the app's one spelling either way.
        path = file_paths.normalize(path)
        self._document = None
        self._site_path = ""
        self.site.hide()
        self.note.hide()
        self.sources.hide()
        generation = self._wait(_("Reading “{file}”…").format(
            file=os.path.basename(path)))
        self._controller.read_file(path, on_done=lambda collection, document, error: (
            self._read(generation, collection, document, error)),
            on_site_needed=lambda: self._ask_site(generation, path))

    def _ask_site(self, generation: int, path: str) -> None:
        """The export writes its blog's address as a placeholder: ask for
        the address once, here, before its posts open."""
        if not self._current(generation):
            return
        self._site_path = path
        self.site.show()
        self._update_buttons()
        self.site_address.setFocus()

    def _site(self) -> str:
        """The typed blog address made whole, or "" while it is none."""
        address = normalize_user_url(self.site_address.text().strip())
        parts = urlsplit(address)
        if parts.scheme in ("http", "https") and "." in (parts.hostname or ""):
            return address.rstrip("/")
        return ""

    def _continue_with_site(self) -> None:
        if self._site() and not self._waiting():
            self._read_with_site(self._site())

    def _read_with_site(self, site: str) -> None:
        path = self._site_path
        if not path or self._waiting():
            return
        generation = self._wait(_("Reading “{file}”…").format(
            file=os.path.basename(path)))
        self._controller.read_file(path, site_url=site, on_done=lambda collection, document,
                                   error: self._read(generation, collection, document, error))

    def _read(self, generation: int, collection, document, error: str) -> None:
        if not self._current(generation):
            return
        if collection is not None:
            self.opened.emit(collection.id)
            self.accept()
            return
        if document is not None:
            if not document.feeds:
                self._fail(NO_SOURCES_IN_LIST)
                return
            self._document = document
            self.note.setText(list_text(len(document.feeds)))
            self.note.show()
            self.sources.setText(list_lines(document))
            self.sources.show()
            self._update_buttons()
            return
        self._fail(error or _("Couldn't read that file."))

    def _follow_all(self) -> None:
        if self._document is None:
            return
        self.followed_list.emit(*self._controller.follow_all(self._document))
        self.accept()


class LinkSheet(_Sheet):
    """Import a Link.

    Signals:
      opened(str)        the link's posts are in the sidebar (its id)
      follow(str)        the link is a whole source: follow it (its address)
    """

    opened = Signal(str)
    follow = Signal(str)

    def __init__(self, controller, *, dark: bool = False, parent=None) -> None:
        super().__init__(controller, _("Import a Link"), dark=dark, parent=parent)
        label = text_label(_("Link to a post, a thread or a file"))
        self.address = QLineEdit()
        self.address.setObjectName("imports_link")
        self.address.setPlaceholderText(_("example.com/my-post  ·  a Bluesky or Nostr "
                                          "link  ·  notes.md"))
        self.address.setClearButtonEnabled(True)
        self.address.setAccessibleName(_("Link to a post, a thread or a file"))
        label.setBuddy(self.address)
        self.address.textChanged.connect(self._edited)
        self.column.addWidget(label)
        self.column.addWidget(self.address)
        self.column.addWidget(text_label(_("It is imported once, as a private draft, and not "
                                           "kept as a source."), "help"))

        self.source_note = QWidget()
        row = QHBoxLayout(self.source_note)
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(12)
        row.addWidget(text_label(_("This looks like a whole source. Follow it to get its new "
                                   "posts too.")), 1)
        self.follow_button = QPushButton(_("Follow…"))
        answer_return_as_the_system_does([self.follow_button])
        self.follow_button.clicked.connect(self._follow)
        row.addWidget(self.follow_button, 0, Qt.AlignmentFlag.AlignTop)
        self.source_note.hide()
        self.column.addWidget(self.source_note)
        self._add_busy()
        self._add_error()
        self._finish()
        self.address.setFocus()

    def _buttons(self) -> list:
        return [("cancel", _("Cancel"), NORMAL, self.reject, True),
                ("open", _("Open"), DEFAULT, self.open_link,
                 bool(self.address.text().strip()) and not self._waiting())]

    def _edited(self, text: str) -> None:
        if self._waiting():
            self._generation += 1
            self.busy.hide()
        self.error.hide()
        address = text.strip()
        whole = bool(address) and (is_list_address(address) or is_whole_source(address))
        self.source_note.setVisible(whole)
        self._update_buttons()

    def open_link(self) -> None:
        address = self.address.text().strip()
        if not address or self._waiting():
            return
        if not can_resolve_source(address):
            self._fail(INVALID_ADDRESS)
            return
        generation = self._wait(_("Opening {address}…").format(address=address))
        self._controller.open_link(address, on_done=lambda collection, error: self._opened(
            generation, collection, error))

    def _opened(self, generation: int, collection, error: str) -> None:
        if not self._current(generation):
            return
        if collection is None:
            self._fail(error or NOTHING_FOUND)
            return
        self.opened.emit(collection.id)
        self.accept()

    def _follow(self) -> None:
        self.follow.emit(self.address.text().strip())
        self.accept()
