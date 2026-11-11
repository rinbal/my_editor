#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
HTML Editor widget with bullet and format logic.
"""

import os
import re
import sys
import functools
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PySide6.QtCore import (
    Qt, QTimer, QMargins, QRect, QRectF, QPoint, QPointF, QMetaMethod, QEvent, QUrl, Signal,
    QByteArray,
)
from PySide6.QtGui import (
    QPainter, QTextBlockFormat, QTextCursor, QTextCharFormat, QColor, QClipboard, QPen,
    QTextOption, QImage, QPixmap, QTextDocument, QTextDocumentFragment, QTextFormat,
    QDesktopServices, QFont, QKeyEvent, QKeySequence,
)
from PySide6.QtWidgets import QTextEdit, QMenu, QApplication, QToolTip, QWidget
from constants import (
    DARK_BG, DARK_FG, LIGHT_BG, LIGHT_FG, DARK_SELECTION, LIGHT_SELECTION,
    DARK_GUIDE, LIGHT_GUIDE, DARK_CURRENT_LINE, LIGHT_CURRENT_LINE, DARK_PAPER, LIGHT_PAPER,
)
from fonts import code_font, writing_font
from i18n import N_, _, ngettext
import announce
from doc_walk import (SOFT_BREAK_PROPERTY, TYPED_TEXT_PROPERTY, block_units, doc_length,
                      doc_offset, iter_blocks, text_index)
import image_safety
import file_paths
import link_url
import mac_words
import markdown_shortcuts
import media_badge
import media_files
import paste
import photo_group
import photo_group_drawing
import photo_group_edit
import photo_group_editing
import photo_group_overlay
import photo_group_view
import rich_text
import text_look
from slash_menu import SlashMenu
import url_safety
from link_popover import LinkPopover, clipboard_address


# Characters per line of prose on Paper Mode's page: within the 50 to 75
# that typography (and nostrdesign.org's UI tips) give for comfortable
# reading. Outside Paper Mode prose runs across the whole width, and this
# is the narrowest it is let to become when side panels need room
# (panel_layout.min_text_width).
WRITING_MEASURE = 72

# Characters per line of code on Paper Mode's page, measured with "0":
# longer than prose, as code lines are written longer.
CODE_PAGE_MEASURE = 88

# Distance from the editor's edges to prose at full width, in logical
# pixels, the document's own margin included: a quiet border, as a page
# has, so the words do not touch the window's edge or a side panel. Code
# keeps only the document's margin, its lines as long as they were written.
PROSE_INSET = 48

# Stand-in painted for an image whose bytes have not arrived yet. Its
# size is deliberately modest: it is replaced in place once the real
# image resolves, and a large box would reflow the whole document twice.
_PLACEHOLDER_SIZE = (160, 100)
_PLACEHOLDER_COLOR = "#c8c8c8"


def _bare_title(title: str) -> str:
    """A menu item's title to say or name a step by: without the
    ellipsis that says it asks first, and the space German puts before it
    ("Fotos hinzufügen …")."""
    return title.rstrip("\u2026").rstrip()


def _n_pictures_left_out(count: int) -> str:
    return ngettext("A picture could not be pasted with the text. Copy it on its own "
                    "and paste it.",
                    "{count} pictures could not be pasted with the text. Copy each on its "
                    "own and paste it.", count).format(count=count)


def _is_document_relative(url) -> bool:
    """Whether a resource name means "beside the document".

    A name with no scheme and no absolute path belongs to the directory
    the file came from. Qt's own answer is the process working
    directory, which is wherever the app happened to be launched from
    and names a different file entirely.
    """
    if url.scheme():
        return False
    name = url.toString()
    return bool(name) and not name.startswith(("/", "\\")) and not os.path.isabs(name)


# A line break inside a paragraph (Shift+Return), and inside a table cell
# (Return), which Markdown writes in a cell as a space.
LINE_BREAK = "\u2028"


# The kinds of run of typing (HtmlEditor._typing), and the pause that ends
# one: as long as a person takes between words while typing on.
TYPED = "typed"
BACKWARD = "backward"
FORWARD = "forward"
TYPING_PAUSE_S = 2.0
TYPING_STEP = N_("Typing")


@dataclass(frozen=True)
class _Run:
    """A run of typing: its kind, where the caret ends it, the Undo count
    after it and when it last went on."""

    kind: str
    end: int
    steps: int
    at: float
    before: int = 0             # Qt's count of changes before the run
    name: str = TYPING_STEP     # what Edit > Undo calls it


def _named_step(name: str):
    """A command as its own step of Undo, called ``name`` (HtmlEditor.undo_step)."""
    def wrap(method):
        @functools.wraps(method)
        def run(self, *args, **kwargs):
            with self.undo_step(name):
                return method(self, *args, **kwargs)
        return run
    return wrap


# What Edit > Undo calls what a Markdown Shortcut makes ("heading 1", ...).
_SHORTCUT_STEPS = {"heading": N_("Paragraph Style"), "bullet": N_("List"),
                   "number": N_("List"), "quote": N_("Quote"), "checklist": N_("Checklist")}

# What Edit > Undo calls a change of each inline style.
_STYLE_STEPS = {"bold": N_("Bold"), "italic": N_("Italic"), "underline": N_("Underline"),
                "strike": N_("Strikethrough"), "code": N_("Code")}

# What Edit > Undo calls a change of indent (Tab and Shift+Tab included):
# the command's own name, by its direction.
_INDENT_STEPS = {1: N_("Increase Indent"), -1: N_("Decrease Indent")}


class _EmptyCopy(QTextEdit):
    """A text field never shown, with nothing in it, whose only use is to
    have Qt make clipboard data (HtmlEditor.createMimeDataFromSelection):
    Qt's own object, which outlives Python, with no copy of a selection
    in it yet."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.hide()

    def qt_data(self):
        return self.createMimeDataFromSelection()


def _open_paragraph_above(cursor: QTextCursor) -> None:
    """A clean paragraph above the one ``cursor`` starts, the cursor in it."""
    cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
    cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)
    cursor.setBlockFormat(QTextBlockFormat())
    cursor.setBlockCharFormat(QTextCharFormat())


# The keys Qt's text field deletes with by itself (on a Mac the Emacs keys
# among them: Control-D, Control-H, Control-K).
_QT_DELETING_KEYS = (
    QKeySequence.StandardKey.Delete, QKeySequence.StandardKey.Backspace,
    QKeySequence.StandardKey.DeleteEndOfLine, QKeySequence.StandardKey.DeleteEndOfWord,
    QKeySequence.StandardKey.DeleteStartOfWord, QKeySequence.StandardKey.DeleteCompleteLine,
)


# The text cursors Settings offers, as the setting stores them: Qt's own
# insertion point at a width in pixels, or a block over the next letter.
CARET_STYLES = ("thin", "medium", "thick", "block")
CARET_WIDTHS = {"thin": 1, "medium": 2, "thick": 4, "block": 0}
DEFAULT_CARET_STYLE = "medium"


class HtmlEditor(QTextEdit):
    """QTextEdit with bullet and format logic."""

    # Media entry points. The editor knows nothing about where an image
    # goes or who uploads it; it reports the gesture and lets whoever is
    # connected decide. Nothing is emitted when nobody is listening, so
    # the widget stays usable on its own (tests, previews).
    image_pasted = Signal(object)    # QImage from the clipboard
    urls_dropped = Signal(list)      # list[QUrl] dropped on the editor (or image files pasted)
    # A short message for the status bar about what the editor just did
    # (a paste that had to leave pictures out).
    notice = Signal(str)
    # The part of the document on screen changed (scrolled, resized):
    # whoever paints only what is visible (find highlights, spelling)
    # paints again.
    visible_area_changed = Signal()
    # "@" typed at the start of a word, where a mention can go: its position.
    mention_requested = Signal(int)
    # What Undo and Redo are called may have changed (undo_name).
    undo_named = Signal()
    # A click on audio or video (its name, where in it the click was,
    # whether it was a double-click): whoever plays it decides whether the
    # click was on its play button.
    media_clicked = Signal(str, QPointF, bool)
    # A picture of a photo group to see in Quick Look (double-click, or
    # Space with it selected), by its name; "" puts Quick Look away.
    picture_look_requested = Signal(str)

    # Kinds of highlights shown over the text, bottom to top: each kind
    # keeps its own (set_highlights), the editor shows them together.
    HIGHLIGHT_LAYERS = ("spelling", "find")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setUndoRedoEnabled(True)
        # What a screen reader calls the text area until the window names
        # it after its document.
        self.setAccessibleName(_("Document"))
        # A soft line break read from a file (markdown_writer) is written
        # back as the line end it was, a character the file escaped as an
        # escape; what is typed after one, a line break with Shift+Return
        # among it, or Markdown, must not take that on.
        self.currentCharFormatChanged.connect(self._drop_read_marks)

        # Who fills the context menu: the window puts its own commands
        # there (the same ones as in its menus). Without one, the menu has
        # Cut, Copy and Paste.
        self._context_menu_filler = None
        # Whether the document can hold Markdown structure (lists,
        # headings): the window answers per tab; on its own, it can.
        self._holds_structure = lambda: True
        # Whether this document's file keeps underline and colors (HTML).
        self._keeps_local_formats = lambda: False
        # Whether this document's file keeps a picture (set_pictures_check).
        self._keeps_pictures = lambda: True
        # What a paste may bring in (set_paste_guard); everything by default.
        self._paste_guard = None
        # Whether typed Markdown turns into what it means
        # (markdown_shortcuts.py): the person's choice, off until made.
        self._markdown_shortcuts = lambda: False
        # The slash menu (slash_menu.py): what it offers for the words after
        # the slash, from the window; where the slash is while it is open.
        self._slash_items = None
        self._slash_menu = None
        self._slash_at = -1
        self._slash_moved = False        # an item chosen with Up or Down
        self._slash_shown = None         # the words the list was made for
        self._slash_said = ""            # the choice screen readers heard last
        self.cursorPositionChanged.connect(self._slash_follow_caret)
        # Who opens a link Command-clicked (Ctrl-clicked elsewhere): the
        # window, which knows where each kind of link goes.
        self._link_opener = None
        # Whether a drop of files brings anything the window takes
        # (set_drop_check); a drop of nothing usable is not accepted.
        self._drop_check = lambda _urls: True
        # Whether a Nostr account is in use: Nostr shows up in the editor
        # only then (nostr/state.py).
        self._nostr_active = lambda: False
        # Whether a web address names a file of the Media Library: pasted
        # on its own, that file goes in, not its address (_pasted_media).
        self._in_library = lambda _address: False
        # Where each picture, audio or video of the text is (media_badge.py):
        # the window answers while a Nostr account is in use; none on its own.
        self._media_badge = lambda _name: None

        # Resource seam: a resolver plus the URL scheme it answers for.
        # Injected by the window so the editor carries no knowledge of
        # what an asset is or where its bytes live.
        self._resource_resolver = None
        # The app's own objects as other apps get them (set_object_lookup).
        self._outside = None
        # The typing run going on (one step of Undo, _typing), and each step
        # of Undo by Qt's count of changes after it: the count before it and
        # what it is called (undo_step). A step can hold several changes.
        self._run: Optional[_Run] = None
        self._undo_names: dict = {}
        self._joined_at = None          # a run's step being joined (_typing)
        # Where Copy's data is made, with nothing selected (createMime...).
        self._empty_copy = None
        # What a paste or a drop is called in Edit > Undo (insertFromMimeData).
        self._mime_step = N_("Paste")
        self.document().undoCommandAdded.connect(self._forget_redone_names)
        self.document().modificationChanged.connect(
            lambda modified: modified or self.end_typing_run())
        self.cursorPositionChanged.connect(self._caret_moved)
        self.selectionChanged.connect(self._caret_moved)
        self._asset_scheme = None
        self._asset_placeholder = None
        self._local_image_resolver = None
        self._web_image_resolver = None

        # Always wrap to the visible viewport width, breaking long unbroken
        # tokens instead of forcing a horizontal scrollbar. This keeps
        # reflow correct across Paper Mode toggles and window resizes.
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)

        self.setStyleSheet(f"""
            QTextEdit {{
                background: {DARK_BG};
                color: {DARK_FG};
                border: none;
                selection-background-color: {DARK_SELECTION};
                padding: 8px;
            }}
        """)
        # Code and plain text are set in the monospace font; writing gets
        # the system's text font (set_writing_font). The font is set here,
        # not in the style sheet, which could only name a family.
        self.setFont(code_font())
        # Room beside the text for the box painted behind a code block
        # (text_look). A margin of the layout only: neither HTML nor
        # Markdown keeps it, and Qt records it as an edit, so it is set
        # with nothing to undo and the new document left unchanged.
        doc = self.document()
        doc.setUndoRedoEnabled(False)
        doc.setDocumentMargin(text_look.CODE_PAD_X + 4)
        doc.setUndoRedoEnabled(True)
        doc.setModified(False)
        # Photo groups (photo_group_view): their pictures sized as readers
        # size them, from the pictures this view loads (loadResource), at
        # every width and height of the window, with nothing in the
        # document changed; room kept above each for the bar shown over it
        # (photo_group_overlay), so showing the bar moves nothing.
        photo_group_view.install(doc, photo_group_view.resource_sizes(doc))
        photo_group_view.set_room_above(doc, photo_group_overlay.BAR_ROOM)
        # Whether Quick Look shows a picture of this document (the window
        # answers): Space and Escape then put it away.
        self._picture_look_open = lambda: False
        # The tab holds prose (set_prose): an inset at full width, and a
        # page of WRITING_MEASURE characters in Paper Mode.
        self._prose = False
        # Line numbers shown beside the text (set_gutter_shown).
        self._gutter_shown = False
        self._highlights = {layer: [] for layer in self.HIGHLIGHT_LAYERS}
        # The same over the captions of photo groups (drawn, not text).
        self._caption_marks = {layer: [] for layer in self.HIGHLIGHT_LAYERS}
        self.document().contentsChange.connect(self._renew_highlights)
        # Pictures fitted to the text's width (_refit_pictures): looked at
        # again once an edit brings one in, or the width changes.
        self._picture_room = 0
        self._refit_timer = QTimer(self)
        self._refit_timer.setSingleShot(True)
        self._refit_timer.setInterval(0)
        self._refit_timer.timeout.connect(self._refit_pictures)
        self.document().contentsChange.connect(self._pictures_maybe_added)
        # A caption changed (its picture's format): its words are checked
        # once the edit is over.
        self._captions_timer = QTimer(self)
        self._captions_timer.setSingleShot(True)
        self._captions_timer.setInterval(0)
        self._captions_timer.timeout.connect(self._draw_spelling)
        self.verticalScrollBar().valueChanged.connect(self.visible_area_changed)
        # Spelling (spelling.DocumentSpelling), while it is checked here.
        self._spelling = None
        self.visible_area_changed.connect(self._spelling_follow_view)
        self.cursorPositionChanged.connect(self._spelling_follow_caret)

        # Track active formatting state for persistent formatting
        self.active_format = {
            'bold': False,
            'italic': False,
            'underline': False,
            'color': None
        }

        # Connect to cursor position changes to update active format
        self.cursorPositionChanged.connect(self._update_active_format)
        # Where the caret is, for a screen reader: a checklist item's state,
        # a table cell's place (announce.py).
        self._spoken_place = None
        self.cursorPositionChanged.connect(self._announce_place)
        # The pictures edited where they stand: their commands, chips,
        # dragging and the ring around the one at the caret.
        self.photos = photo_group_editing.PhotoEditing(self)

        # The text cursor chosen in Settings (set_caret_style): Qt's own
        # insertion point at a width, or a block drawn here.
        self._caret_style = DEFAULT_CARET_STYLE
        self.setCursorWidth(CARET_WIDTHS[DEFAULT_CARET_STYLE])
        self._cursor_visible = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(530)
        self._blink_timer.timeout.connect(self._on_cursor_blink)
        self._blink_timer.start()

        # Background viewing aids (all painted behind the text in paintEvent).
        self._bg_pattern = "none"        # none | lines | dashed | dots | grid
        self._paper_mode = False
        self._highlight_line = False
        self._paper_measure_chars = CODE_PAGE_MEASURE   # code's page in Paper Mode
        # Room above the text for a header shown there (editor_header.py).
        self._top_inset = 0
        self.viewport().setAutoFillBackground(False)

    def undo(self):
        self.end_typing_run()
        count = self.document().availableUndoSteps()
        with self.photos.quietly():
            self.document().undo()
        self.photos.came_back(count, redo=False)

    def redo(self):
        self.end_typing_run()
        count = self.document().availableUndoSteps()
        with self.photos.quietly():
            self.document().redo()
        self.photos.came_back(count, redo=True)

    # -------- Steps of Undo --------
    # A run of typing is one step of Undo, as in TextEdit and Pages: the
    # characters typed one after another where the caret is, Return among
    # them, and a run of Backspace (or of Delete) is one too. A run ends
    # when the caret or the selection moves, when anything else changes the
    # text (a command, a paste, a format, Markdown Shortcuts, Undo), when
    # the person pauses for TYPING_PAUSE_S, when the document is saved, and
    # when typing turns into deleting or back.

    @contextmanager
    def _typing(self, kind: str, cursor: Optional[QTextCursor] = None):
        """A cursor at the caret to type (``kind`` TYPED) or delete
        (BACKWARD, FORWARD) with: in the run going on, when this goes on
        with it, else in a new one. What changes the text meanwhile, by
        this cursor or by Qt's own handling of a key or of an input
        method, is in the run; a key that changes nothing leaves the run
        and the name of the step before as they were."""
        doc = self.document()
        changes = []

        def changed(*_where) -> None:
            changes.append(_where)

        doc.contentsChange.connect(changed)
        cursor = cursor or self.textCursor()
        run = self._run
        goes_on = (run is not None and run.kind == kind and not cursor.hasSelection()
                   and run.end == cursor.position()
                   and run.steps == doc.availableUndoSteps()
                   and time.monotonic() - run.at < TYPING_PAUSE_S)
        before = run.before if goes_on else doc.availableUndoSteps()
        if goes_on:
            cursor.joinPreviousEditBlock()
            # Qt reports a joined block that ends with nothing in it as a
            # new step: _forget_redone_names knows it is none.
            self._joined_at = run.steps
        else:
            cursor.beginEditBlock()
        try:
            yield cursor
        finally:
            cursor.endEditBlock()
            self._joined_at = None
            doc.contentsChange.disconnect(changed)
            # A photo group the run changed is whole again within its step
            # (photo_group_edit); only a deletion lets pictures that became
            # neighbours join, never words being typed.
            photo_group_edit.settle(doc, changes, cursor, join=kind != TYPED)
            if changes:
                name = run.name if goes_on else TYPING_STEP
                self._run = _Run(kind, cursor.position(), doc.availableUndoSteps(),
                                 time.monotonic(), before, name)
                self._record_step(before, name)
                self.setTextCursor(cursor)
                self.undo_named.emit()

    def end_typing_run(self) -> None:
        """What is typed next is a step of Undo of its own."""
        self._run = None

    def type_into_last_step(self) -> None:
        """What is typed next at the caret is part of the step just made,
        under its name (the first words written into the line Split Group
        Here opened). It waits for them as long as the caret stays."""
        now = self.document().availableUndoSteps()
        before, name = self._undo_names.get(now, (now, TYPING_STEP))
        self._run = _Run(TYPED, self.textCursor().position(), now, float("inf"), before, name)

    def named_step(self, after: int) -> Optional[Tuple[int, str]]:
        """The step of Undo that ends at Qt's count ``after``: the count
        before it and its name; None when it has none."""
        return self._undo_names.get(after)

    def forget_step(self, after: int) -> None:
        """The step that ended at Qt's count ``after`` is gone (taken back
        and dropped): its name with it."""
        if self._undo_names.pop(after, None) is not None:
            self.undo_named.emit()

    def _caret_moved(self) -> None:
        run = self._run
        cursor = self.textCursor()
        if run is not None and (cursor.position() != run.end or cursor.hasSelection()):
            self._run = None

    @contextmanager
    def undo_step(self, name: str):
        """A command's change as its own step of Undo, one step whatever it
        changes, called ``name`` (an N_ word: "Paste", "Bold"), which Edit >
        Undo shows where the platform names it."""
        self.end_typing_run()
        before = self.document().availableUndoSteps()
        whole = QTextCursor(self.document())
        try:
            with self._groups_kept():
                whole.beginEditBlock()
                try:
                    yield
                finally:
                    whole.endEditBlock()
        finally:
            self._name_step(before, name)

    @contextmanager
    def _groups_kept(self):
        """What an edit block inside this changes leaves every photo group
        whole (photo_group_edit.settle), in the block's own step of Undo.
        A change of format alone (the same characters, in italic now)
        joins no pictures."""
        doc = self.document()
        changes = []

        def changed(*where) -> None:
            changes.append(where)

        # The document says what changed when an edit block ends.
        doc.contentsChange.connect(changed)
        try:
            yield
        finally:
            doc.contentsChange.disconnect(changed)
            caret = self.textCursor()
            if photo_group_edit.settle(
                    doc, changes, caret,
                    join=any(removed != added for _at, removed, added in changes)) \
                    and caret.position() != self.textCursor().position():
                self.setTextCursor(caret)

    def _name_step(self, before: int, name: str) -> None:
        """Call the step made since Qt's count of changes was ``before``
        ``name``, when there is one; what is typed next is a run of its own."""
        if self.document().availableUndoSteps() > before and name:
            self._record_step(before, name)
        self.end_typing_run()
        self.undo_named.emit()

    def _record_step(self, before: int, name: str) -> None:
        """The step from Qt's count of changes ``before`` to now is called
        ``name``. A name kept for a count inside it was another step's,
        undone and replaced by this one (or an earlier part of this run of
        typing): it is forgotten, so Redo never names it."""
        after = self.document().availableUndoSteps()
        self._undo_names = {steps: step for steps, step in self._undo_names.items()
                            if not before < steps < after}
        self._undo_names[after] = (before, name)
        # A photo step it goes on from or holds is this step now.
        self.photos.steps.recorded(before, after)

    def undo_name(self) -> str:
        """What Undo would take back (an N_ word, "" when not known)."""
        return next(iter(self.undo_history(1)), "")

    def redo_name(self) -> str:
        """What Redo would bring back (an N_ word, "" when not known)."""
        return next(iter(self.redo_history(1)), "")

    def undo_history(self, limit: int = 0) -> List[str]:
        """The names of the steps Undo takes back one after another (N_
        words), newest first, as far as they are known; ``limit`` of them
        at most when given. Each step starts where the one before it ends."""
        names = []
        now = self.document().availableUndoSteps()
        while now in self._undo_names and (not limit or len(names) < limit):
            now, name = self._undo_names[now]
            names.append(name)
        return names

    def redo_history(self, limit: int = 0) -> List[str]:
        """The names of the steps Redo brings back one after another, the
        next first, by the same rules as undo_history: each starts where
        the document is after the one before."""
        now = self.document().availableUndoSteps()
        ahead = {before: (after, name) for after, (before, name)
                 in sorted(self._undo_names.items()) if after > now}
        names = []
        while now in ahead and (not limit or len(names) < limit):
            now, name = ahead[now]
            names.append(name)
        return names

    def _forget_redone_names(self) -> None:
        """A new change drops what could be redone, and with it its names.
        A run's block joined and ended with nothing in it (Tab that only
        goes to the next cell, Backspace at the start) is no change."""
        now = self.document().availableUndoSteps()
        if now == self._joined_at:
            return
        self._undo_names = {steps: name for steps, name in self._undo_names.items()
                            if steps < now}

    # -------- Resource resolution --------
    def set_resource_resolver(self, resolver, scheme: str) -> None:
        """Install the callback that turns a ``scheme:`` name into a QImage.

        ``resolver`` may return None; the placeholder is shown then.
        """
        self._resource_resolver = resolver
        self._asset_scheme = scheme or None

    def set_local_image_resolver(self, resolver) -> None:
        """Install the callback that reads an image named beside the file.

        ``resolver`` takes the name exactly as the document spells it
        and returns a QImage or None. Injected so the editor holds no
        opinion about where the document lives or how bytes decode.
        """
        self._local_image_resolver = resolver

    def set_web_image_resolver(self, resolver) -> None:
        """Install the callback that finds an image named by a web address
        (an article or draft reopened, a picture pasted from a page).

        ``resolver`` takes the address and returns a QImage, or None while
        it is on its way: the placeholder shows, and the window puts the
        picture in its place when it arrives.
        """
        self._web_image_resolver = resolver

    def _placeholder_image(self) -> QImage:
        """One shared stand-in image, built on first use."""
        if self._asset_placeholder is None:
            image = QImage(*_PLACEHOLDER_SIZE, QImage.Format_RGB32)
            image.fill(QColor(_PLACEHOLDER_COLOR))
            # Its shape is not the picture's: a group lays it out at 3:2.
            self._asset_placeholder = photo_group_drawing.mark_placeholder(image)
        return self._asset_placeholder

    def loadResource(self, type_, url):
        """Resolve a document resource, keeping asset names off the disk.

        An asset name is never passed to Qt's default resolution: Qt
        would treat it as a relative file path. A miss returns the
        placeholder rather than nothing, because Qt caches an image but
        re-asks for a null result on every repaint.

        A picture asked for as wide as the page (#wide at the end of its
        address, photo_group) is the picture without it.

        A document-relative image goes to the local resolver for the
        same reason it never falls through: Qt's fallback reads the
        name against the working directory, which is not the folder the
        picture was saved beside.
        """
        url = photo_group.without_wide_url(url)
        if self._asset_scheme is not None and url.scheme() == self._asset_scheme:
            if type_ != QTextDocument.ImageResource:
                return None
            image = None
            if self._resource_resolver is not None:
                image = self._resource_resolver(url.toString())
            return self.fitted_picture(image) if image is not None else self._placeholder_image()
        if (type_ == QTextDocument.ImageResource
                and self._local_image_resolver is not None
                and _is_document_relative(url)):
            image = self._local_image_resolver(url.toString())
            return self.fitted_picture(image) if image is not None else self._placeholder_image()
        if (type_ == QTextDocument.ImageResource
                and self._web_image_resolver is not None
                and url.scheme().lower() in ("http", "https")):
            # Qt's own resolution never fetches an address: the picture
            # would stay blank, and be asked for again on every repaint.
            image = self._web_image_resolver(url.toString())
            return self.fitted_picture(image) if image is not None else self._placeholder_image()
        found = super().loadResource(type_, url)
        if type_ == QTextDocument.ImageResource and isinstance(found, (QImage, QPixmap)):
            return self.fitted_picture(found)
        return found

    # -------- Pictures fitted to the text --------
    # Qt draws a picture in the text at its full pixel size: a photo from a
    # phone, 4000 pixels across, ran far past the page. As Pages and Notes
    # do, a picture wider than the text is shown at the text's width, its
    # height in proportion; a smaller one keeps its own size. Only how it
    # is shown changes: the picture keeps every pixel (it is drawn at a
    # higher pixel ratio, sharp on any screen), and neither the document
    # nor its undo history is touched, so nothing is saved differently.

    def _room_for_pictures(self) -> int:
        """The width a picture may take: the text's, inside the
        document's margins."""
        margin = self.document().documentMargin()
        return max(80, int(self.viewport().width() - 2 * margin))

    def fitted_picture(self, image):
        """``image`` (a QImage or QPixmap) as the text shows it: at most
        as wide as the text, all of its pixels kept."""
        if isinstance(image, QPixmap):
            image = image.toImage()
        if not isinstance(image, QImage) or image.isNull():
            return image
        room = self._room_for_pictures()
        ratio = image.width() / room if image.width() > room else 1.0
        if abs(image.devicePixelRatio() - ratio) < 1e-6:
            return image
        fitted = QImage(image)
        fitted.setDevicePixelRatio(ratio)
        return fitted

    def add_picture_resource(self, name: str, payload) -> None:
        """Give the document the picture called ``name``, fitted to the
        text. ``payload`` is a QImage, a QPixmap or the file's bytes; bytes
        that do not decode are handed over as they are, for Qt to try."""
        image = payload
        if isinstance(payload, (bytes, bytearray, QByteArray)):
            image = image_safety.decode_image_bytes(bytes(payload))
            if image is None:
                self.document().addResource(QTextDocument.ImageResource, QUrl(name), payload)
                return
        self.document().addResource(QTextDocument.ImageResource, QUrl(name),
                                    self.fitted_picture(image))
        # The groups that show it are laid out again by the size now known.
        photo_group_view.picture_arrived(self.document(), name)

    def set_picture_look_check(self, is_open) -> None:
        """``is_open()`` says whether Quick Look shows a picture of this
        document (the window keeps the panel)."""
        self._picture_look_open = is_open

    def _pictures_maybe_added(self, position: int, _removed: int, added: int) -> None:
        """An edit that brought a picture in (typed text never does) has
        it fitted once the edit is over; one in a group's paragraph (a
        caption changed) has its spelling looked at again."""
        if self._spelling is not None and photo_group.is_group_block(
                self.document().findBlock(position)):
            self._captions_timer.start()
        if added <= 0:
            return
        cursor = QTextCursor(self.document())
        end = min(position + added, max(0, self.document().characterCount() - 1))
        cursor.setPosition(min(position, end))
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        if "\ufffc" in cursor.selectedText():
            self._refit_timer.start()

    def _refit_pictures(self) -> None:
        """Fit every picture of the document to the text's width now:
        after the width changed, or pictures came in. Pictures with a size
        of their own (the cards of audio and video) are left as they are."""
        doc = self.document()
        self._picture_room = self._room_for_pictures()
        changed = False
        seen = set()
        for block in iter_blocks(doc):
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                it += 1
                fmt = fragment.charFormat()
                if not fmt.isImageFormat():
                    continue
                image_fmt = fmt.toImageFormat()
                name = image_fmt.name()
                if name in seen or image_fmt.hasProperty(QTextFormat.ImageWidth):
                    continue
                seen.add(name)
                found = doc.resource(QTextDocument.ImageResource, QUrl(name))
                if isinstance(found, (QByteArray, bytes, bytearray)):
                    image = image_safety.decode_image_bytes(bytes(found))
                elif isinstance(found, QPixmap):
                    image = found.toImage()
                else:
                    image = found
                if not isinstance(image, QImage) or image.isNull():
                    continue
                fitted = self.fitted_picture(image)
                if fitted is not found:
                    doc.addResource(QTextDocument.ImageResource, QUrl(name), fitted)
                    changed = True
        if changed:
            doc.markContentsDirty(0, doc.characterCount())

    # -------- Format Toggles --------
    def toggle_bold(self):
        self.active_format['bold'] = self.toggle_style(rich_text.BOLD)

    def toggle_italic(self):
        self.active_format['italic'] = self.toggle_style(rich_text.ITALIC)

    def toggle_underline(self):
        self.active_format['underline'] = self.toggle_style(rich_text.UNDERLINE)

    @_named_step(N_("Color"))
    def apply_color(self, qcolor: QColor | None):
        cursor = self.textCursor()
        if qcolor is None:
            if cursor.hasSelection():
                # mergeCharFormat can only set properties, not clear them.
                # Must iterate character by character to actually remove the color.
                start, end = cursor.selectionStart(), cursor.selectionEnd()
                c = QTextCursor(self.document())
                c.beginEditBlock()
                c.setPosition(start)
                while c.position() < end:
                    c.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
                    char_fmt = c.charFormat()
                    char_fmt.clearForeground()
                    c.setCharFormat(char_fmt)
                    c.setPosition(c.position())
                c.endEditBlock()
            else:
                char_fmt = self.currentCharFormat()
                char_fmt.clearForeground()
                self.setCurrentCharFormat(char_fmt)
        else:
            fmt = QTextCharFormat()
            fmt.setForeground(qcolor)
            if cursor.hasSelection():
                cursor.mergeCharFormat(fmt)
            else:
                self.mergeCurrentCharFormat(fmt)
        self.active_format['color'] = qcolor
        self.ensureCursorVisible()

    def toggle_style(self, style: str) -> bool:
        """Turn an inline style (rich_text.INLINE) on or off: over the
        whole selection, or for what is typed next. Returns the new state."""
        with self.undo_step(_STYLE_STEPS.get(style, "")):
            return self._toggle_style(style)

    def _toggle_style(self, style: str) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            on = rich_text.toggle_style(cursor, style)
            self.setTextCursor(cursor)
            return on
        on = not rich_text.has_style(self.currentCharFormat(), style)
        if on or style != rich_text.CODE:
            self.mergeCurrentCharFormat(rich_text.style_format(style, on))
        else:
            fmt = self.currentCharFormat()
            fmt.setFontFixedPitch(False)
            fmt.clearProperty(QTextFormat.Property.FontFamilies)
            fmt.clearProperty(QTextFormat.Property.FontFamily)
            self.setCurrentCharFormat(fmt)
        return on

    @_named_step(N_("Paragraph Style"))
    def set_heading(self, level: int) -> None:
        """Body (0) or Heading 1 to 3 for the paragraphs under the cursor;
        the style they already have turns them back into Body."""
        cursor = self.textCursor()
        rich_text.set_heading(cursor, level)
        self.setTextCursor(cursor)
        now = cursor.block().blockFormat().headingLevel()
        self.setCurrentCharFormat(rich_text.restyled(self.currentCharFormat(), now))
        self._update_active_format()

    def toggle_strike(self):
        return self.toggle_style(rich_text.STRIKE)

    def toggle_code(self):
        return self.toggle_style(rich_text.CODE)

    @_named_step(N_("Clear Formatting"))
    def reset_to_default(self):
        """Clear Formatting: no bold, italic, underline, strikethrough,
        inline code or color, over the selection or for what is typed
        next. A link stays a link."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            rich_text.clear_formatting(cursor)
            self.setTextCursor(cursor)
        else:
            self.setCurrentCharFormat(rich_text.typing_format_without(self.currentCharFormat()))

        self.active_format['bold'] = False
        self.active_format['italic'] = False
        self.active_format['underline'] = False
        self.active_format['color'] = None
        self.ensureCursorVisible()

    def _update_active_format(self):
        """Update active format state based on current cursor position."""
        self._leave_link_at_end()
        fmt = self.currentCharFormat()
        self.active_format['bold'] = (fmt.fontWeight() > 400)
        self.active_format['italic'] = fmt.fontItalic()
        self.active_format['underline'] = fmt.fontUnderline()
        self.active_format['color'] = fmt.foreground().color() if fmt.hasProperty(QTextCharFormat.ForegroundBrush) else None

    # -------- Text cursor --------
    def set_caret_style(self, style: str) -> None:
        """The text cursor, as chosen in Settings: one of CARET_STYLES, in
        every document whatever its font (an unknown one is the default)."""
        self._caret_style = style if style in CARET_STYLES else DEFAULT_CARET_STYLE
        self.setCursorWidth(CARET_WIDTHS.get(self._caret_style, 0))
        self.viewport().update()

    def caret_style(self) -> str:
        return self._caret_style

    def block_caret_shown(self) -> bool:
        """Whether the block cursor is drawn now: chosen in Settings, while
        the text has the focus and nothing is selected."""
        return (self._caret_style == "block" and self.hasFocus()
                and self._cursor_visible and not self.textCursor().hasSelection())

    def _block_cursor_rect(self):
        """Return the rect the block cursor occupies (used for painting and invalidation)."""
        rect = self.cursorRect()
        rect.setWidth(max(self.fontMetrics().averageCharWidth(), 10))
        return rect

    def _on_cursor_blink(self):
        self._cursor_visible = not self._cursor_visible
        self.viewport().update(self._block_cursor_rect())

    # -------- Background viewing aids --------
    def set_background_pattern(self, name: str):
        self._bg_pattern = name or "none"
        self.viewport().update()

    def set_highlight_current_line(self, on: bool):
        self._highlight_line = bool(on)
        self.viewport().update()

    def _drop_read_marks(self, fmt: QTextCharFormat) -> None:
        """Keep the marks of what was read from the file on it, off what
        is typed at the caret after it: a soft line break's
        (SOFT_BREAK_PROPERTY) and an escaped character's
        (TYPED_TEXT_PROPERTY)."""
        marks = [mark for mark in (SOFT_BREAK_PROPERTY, TYPED_TEXT_PROPERTY)
                 if fmt.hasProperty(mark)]
        if marks and not self.textCursor().hasSelection():
            plain = QTextCharFormat(fmt)
            for mark in marks:
                plain.clearProperty(mark)
            self.setCurrentCharFormat(plain)

    def set_paper_mode(self, on: bool):
        self._paper_mode = bool(on)
        self._update_paper_margins()
        self.viewport().update()

    def set_writing_font(self, writing: bool, font: Optional[QFont] = None) -> None:
        """Writing (Markdown, drafts): a text font at a reading size.
        Otherwise (code, plain text): the monospace font. ``font``: the one
        chosen (Settings: the writing font, and the app's text size); the
        defaults when not given. How wide the text runs is apart from it
        (set_prose)."""
        self.setFont(font if font is not None else (writing_font() if writing else code_font()))
        self._update_paper_margins()
        self.viewport().update()

    def set_prose(self, on: bool) -> None:
        """The tab holds prose (``on``), in either font. Prose runs across
        the whole width with a quiet border (PROSE_INSET); in Paper Mode it
        is set on a page of WRITING_MEASURE characters of its font. Code
        runs across the whole width, its lines as long as they were
        written, and on a wider page in Paper Mode."""
        self._prose = bool(on)
        self._update_paper_margins()
        self.viewport().update()

    def set_gutter_shown(self, on: bool) -> None:
        """Line numbers are shown at the left (``on``): the column starts
        beside them, as Xcode keeps its line numbers next to the text,
        instead of in the middle of the window, far from them."""
        self._gutter_shown = bool(on)
        self._update_paper_margins()

    def set_top_inset(self, height: int) -> None:
        """Room above the text, in pixels, for a header shown there
        (editor_header.EditorHeader); it is not part of the document."""
        height = max(0, int(height))
        if height != self._top_inset:
            self._top_inset = height
            self._update_paper_margins()

    def top_inset(self) -> int:
        return self._top_inset

    def _update_paper_margins(self):
        """Where the text runs: on Paper Mode's page, centred, or beside
        the line numbers while they are shown; else across the whole
        width, prose with its quiet border (PROSE_INSET), which starts at
        the line numbers while they are shown. Below a header's room, if
        any."""
        doc_margin = self.document().documentMargin()
        if self._paper_mode:
            metrics = self.fontMetrics()
            if self._prose:
                chars, char_w = WRITING_MEASURE, metrics.averageCharWidth()
            else:
                chars, char_w = self._paper_measure_chars, metrics.horizontalAdvance("0")
            # The line is ``chars`` long, the document's margins beside it,
            # in the room the frame and a scroll bar leave.
            measure = (char_w or 8) * chars + 2 * doc_margin
            now = self.viewportMargins()
            room = self.viewport().width() + now.left() + now.right()
            left = right = int(max(0, (room - measure) / 2))
        elif self._prose:
            left = right = int(max(0, PROSE_INSET - doc_margin))
        else:
            left = right = 0
        if self._gutter_shown:
            # Beside the numbers, as Xcode keeps them next to the text;
            # the page keeps its width, its room all at the right.
            if self._paper_mode:
                right += left
            left = 0
        margins = QMargins(left, self._top_inset, right, 0)
        if margins != self.viewportMargins():
            self.setViewportMargins(margins)
            # Qt lays the text out again when the editor is resized, not
            # its viewport: laid out to the new width now, or its lines
            # run on past the column, cut off, until the window is resized.
            self.setLineWrapColumnOrWidth(self.lineWrapColumnOrWidth())
        # Pictures follow the new width of the text.
        if getattr(self, "_refit_timer", None) is not None \
                and self._room_for_pictures() != self._picture_room:
            self._refit_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # A group's rows are never taller than their share of the window,
        # as on a reader's screen (photo_group_view).
        photo_group_view.set_screen_height(self.document(), self.viewport().height())
        if self._paper_mode:
            self._update_paper_margins()
        if self._room_for_pictures() != self._picture_room:
            self._refit_timer.start()
        self.visible_area_changed.emit()

    # -------- Highlights over the text --------
    def set_highlights(self, layer: str, selections, captions=()) -> None:
        """Show ``selections`` (QTextEdit.ExtraSelection) as the highlights
        of one kind (HIGHLIGHT_LAYERS), and ``captions``
        (photo_group_drawing.CaptionMark) as that kind's over the captions of
        photo groups, in place of that kind's earlier ones; the other kinds
        stay. Each highlight holds a cursor the document moves on every
        edit, so callers keep them to what is on screen (visible_range)."""
        self._highlights[layer] = list(selections)
        self.setExtraSelections([s for name in self.HIGHLIGHT_LAYERS
                                 for s in self._highlights[name]])
        if captions or self._caption_marks[layer]:
            self._caption_marks[layer] = list(captions)
            photo_group_view.set_marks(self.document(), [
                mark for name in self.HIGHLIGHT_LAYERS for mark in self._caption_marks[name]])
            self.viewport().update()

    def highlights(self, layer: str) -> list:
        return list(self._highlights[layer])

    def caption_highlights(self, layer: str) -> list:
        """The marks of one kind over the captions of photo groups."""
        return list(self._caption_marks[layer])

    def clear_highlights(self) -> None:
        """No highlights, before the whole text is replaced (a file opened
        or reloaded, a draft put in). Qt fills a document read from
        Markdown or HTML in many steps, each a change; renewing highlights
        at each one (_renew_highlights) sets selections into a document
        still being built, and Qt crashes on it. Those who made them (the
        spelling, a search) make them again from the new text."""
        if any(self._highlights.values()) or any(self._caption_marks.values()):
            for layer in self.HIGHLIGHT_LAYERS:
                self._highlights[layer] = []
                self._caption_marks[layer] = []
            self.setExtraSelections([])
            photo_group_view.set_marks(self.document(), ())

    def _renew_highlights(self, *_change) -> None:
        """Right after every change, every highlight gets a new cursor
        where the change left its own. Qt moves a highlight's end into the
        cells a table gains right after it (Add Row Above, Add Column
        Before, with a word underlined above the table), and then crashes
        painting it, or changing the table again (Delete Row), whichever
        comes first; a cursor made afresh at the same place does neither."""
        if not any(self._highlights.values()):
            return
        doc = self.document()
        for layer in self.HIGHLIGHT_LAYERS:
            renewed = []
            for old in self._highlights[layer]:
                # A highlight is words of one paragraph (a misspelling, a
                # match): Qt can move its end into a table inserted right
                # after it, so it ends where its paragraph does at most.
                start = min(old.cursor.anchor(), old.cursor.position())
                end = max(old.cursor.anchor(), old.cursor.position())
                block = doc.findBlock(start)
                end = min(end, block.position() + block.length() - 1)
                cursor = QTextCursor(doc)
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                fresh = QTextEdit.ExtraSelection()
                fresh.cursor = cursor
                fresh.format = old.format
                renewed.append(fresh)
            self._highlights[layer] = renewed
        self.setExtraSelections([s for name in self.HIGHLIGHT_LAYERS
                                 for s in self._highlights[name]])

    def block_at(self, point: QPoint):
        """The paragraph at ``point`` (viewport coordinates).

        Qt lays a long document out a part at a time, and until it has
        finished, a position on screen can map to the wrong paragraph (the
        top of a fresh 10,000-line document mapped to its 250th line), so
        the layout is completed first. That costs nothing once it is done:
        an edit only lays out what changed. Everything that maps the screen
        to the text (Find's highlights, the line numbers, the quote bars
        and the ruled lines) asks here."""
        self.document().documentLayout().documentSize()
        return self.cursorForPosition(point).block()

    def first_visible_block(self):
        """The paragraph at the top of the viewport."""
        return self.block_at(QPoint(0, 0))

    # -------- Spelling --------
    def set_spelling(self, spelling) -> None:
        """Check this document's spelling with ``spelling`` (a
        spelling.DocumentSpelling of its document), or stop (None): the
        misspelled words on screen are underlined the platform's way."""
        old = self._spelling
        if old is spelling:
            return
        if old is not None:
            try:
                old.misspellingsChanged.disconnect(self._spelling_changed)
            except (RuntimeError, TypeError):
                pass
        self._spelling = spelling
        if spelling is None:
            self.set_highlights("spelling", [])
            return
        spelling.misspellingsChanged.connect(self._spelling_changed)
        spelling.set_cursor_position(self.textCursor().position())
        self._spelling_follow_view()

    def spelling(self):
        """The document's spelling while it is checked, else None."""
        return self._spelling

    def visible_blocks(self) -> tuple:
        """(first, last): the numbers of the blocks on screen."""
        first, last = self.visible_range()
        doc = self.document()
        return (doc.findBlock(first).blockNumber(),
                doc.findBlock(max(first, last - 1)).blockNumber())

    def _spelling_follow_caret(self) -> None:
        if self._spelling is not None:
            self._spelling.set_cursor_position(self.textCursor().position())

    def _spelling_follow_view(self) -> None:
        if self._spelling is None:
            return
        first, last = self.visible_blocks()
        self._spelling.set_visible_blocks(first, last)
        self._draw_spelling()

    def _spelling_changed(self, first: int, last: int) -> None:
        shown_first, shown_last = self.visible_blocks()
        if last >= shown_first and first <= shown_last:
            self._draw_spelling()

    def _draw_spelling(self) -> None:
        """Underline the misspelled words of the blocks on screen."""
        spelling = self._spelling
        if spelling is None:
            return
        underline = QTextCharFormat()
        underline.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        underline.setUnderlineColor(QColor("#E5484D"))
        first, last = self.visible_blocks()
        doc = self.document()
        selections, captions = [], []
        block = doc.findBlockByNumber(first)
        while block.isValid() and block.blockNumber() <= last:
            for word in spelling.misspellings(block):
                mark = QTextEdit.ExtraSelection()
                mark.cursor = QTextCursor(doc)
                mark.cursor.setPosition(block.position() + word.start)
                mark.cursor.setPosition(block.position() + word.end,
                                        QTextCursor.MoveMode.KeepAnchor)
                mark.format = underline
                selections.append(mark)
            if photo_group.is_group_block(block):
                captions.extend(self._caption_misspellings(block, underline))
            block = block.next()
        self.set_highlights("spelling", selections, captions)

    def _caption_misspellings(self, block, underline) -> list:
        """The misspelled words of the captions of a group's paragraph,
        as marks over them."""
        marks = []
        for start, units in photo_group.unit_stretches(block):
            for offset, unit in enumerate(units):
                if not unit.has_caption:
                    continue
                for word in self._spelling.misspellings_of(unit.caption_text):
                    cursor = QTextCursor(self.document())
                    cursor.setPosition(start + offset)
                    marks.append(photo_group_drawing.CaptionMark(cursor, word.start, word.end,
                                                              underline))
        return marks

    @_named_step(N_("Correction"))
    def replace_misspelling(self, misspelling, block_position: int, words: str) -> None:
        """A suggestion for a misspelled word, in its place and in its
        format, as one step to undo."""
        cursor = QTextCursor(self.document())
        cursor.setPosition(block_position + misspelling.start)
        cursor.setPosition(block_position + misspelling.end, QTextCursor.MoveMode.KeepAnchor)
        fmt = cursor.charFormat()
        cursor.beginEditBlock()
        cursor.insertText(words, fmt)
        cursor.endEditBlock()
        self.setTextCursor(cursor)

    def show_document_rect(self, rect: Optional[QRectF]) -> None:
        """Scroll the least that brings ``rect`` (in the document's
        coordinates; a picture, its caption) into view, not at all when it
        is in view; the caret's place for no rect."""
        if rect is None:
            self.ensureCursorVisible()
            return
        bar = self.verticalScrollBar()
        height = self.viewport().height()
        top = rect.top() - bar.value()
        if top < 0 or rect.height() > height:
            bar.setValue(int(rect.top()))
        elif top + rect.height() > height:
            bar.setValue(int(rect.bottom() - height + 1))

    def visible_range(self):
        """``(first, last)``: the document positions from the start of the
        first paragraph on screen to the end of the last."""
        viewport = self.viewport()
        top = self.first_visible_block()
        bottom = self.block_at(QPoint(viewport.width() - 1, viewport.height() - 1))
        return top.position(), bottom.position() + bottom.length()

    def _is_dark(self) -> bool:
        return hasattr(self, "_theme_colors") and self._theme_colors["bg"] == DARK_BG

    def surface_color(self) -> QColor:
        """What the text is set on: the page in Paper Mode, else the
        editor's background."""
        return self._surface_color()

    def _surface_color(self) -> QColor:
        if self._paper_mode:
            return DARK_PAPER if self._is_dark() else LIGHT_PAPER
        bg = self._theme_colors["bg"] if hasattr(self, "_theme_colors") else DARK_BG
        return QColor(bg)

    def paintEvent(self, event):
        vp = self.viewport()
        painter = QPainter(vp)

        # We own the background now (the stylesheet sets it transparent), so fill
        # it ourselves. This is what lets the guides sit behind the text.
        painter.fillRect(vp.rect(), self._surface_color())

        if (self._highlight_line and self.hasFocus() and self.photos.current() is None
                and not self.textCursor().hasSelection()):
            line_rect = self.cursorRect()
            band = QRect(0, line_rect.top(), vp.width(), line_rect.height())
            painter.fillRect(band, DARK_CURRENT_LINE if self._is_dark() else LIGHT_CURRENT_LINE)

        if self._bg_pattern != "none" or self._paper_mode:
            self._paint_guides(painter)

        dark = self._is_dark()
        text_look.paint_code_boxes(self, painter, text_look.code_box_color(dark))
        text_look.paint_quote_bars(self, painter, text_look.quote_bar_color(dark))
        painter.end()

        # (A picture of a group selected: drawn without Qt's highlight.)
        if not self.photos.pointer.paint_text(event):
            super().paintEvent(event)

        p2 = QPainter(vp)
        try:
            media_badge.paint(self, p2, self._media_badge)
            # The ring around the picture at the caret, the chips on the
            # one under the pointer, a picture being moved (photo_group_pointer).
            self.photos.paint(p2)
            # Block cursor on top of the text, in code and plain text.
            if self.block_caret_shown():
                color = QColor(212, 212, 212, 210) if self._is_dark() else QColor(51, 51, 51, 210)
                p2.fillRect(self._block_cursor_rect(), color)
        finally:
            p2.end()

    def _paint_guides(self, painter):
        vp = self.viewport()
        guide = DARK_GUIDE if self._is_dark() else LIGHT_GUIDE
        padding = 8  # matches the stylesheet padding on the editor
        width = vp.width()
        height = vp.height()

        solid = QPen(guide)
        solid.setCosmetic(True)
        solid.setWidth(1)

        pattern = self._bg_pattern
        if pattern in ("lines", "dashed", "grid", "dots"):
            line_ys, pitch = self._rule_line_ys(height)
        else:
            line_ys, pitch = [], 0

        if pattern in ("lines", "dashed"):
            pen = QPen(guide)
            pen.setCosmetic(True)
            pen.setWidth(1)
            if pattern == "dashed":
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            for y in line_ys:
                painter.drawLine(padding, y, width - padding, y)

        elif pattern in ("grid", "dots") and pitch > 0 and line_ys:
            xs = list(range(padding, width - padding + 1, pitch))
            if pattern == "grid":
                painter.setPen(solid)
                for y in line_ys:
                    painter.drawLine(padding, y, width - padding, y)
                top, bottom = line_ys[0], line_ys[-1]
                for x in xs:
                    painter.drawLine(x, top, x, bottom)
            else:  # dots
                painter.setPen(Qt.NoPen)
                painter.setBrush(guide)
                for y in line_ys:
                    for x in xs:
                        painter.drawEllipse(QPoint(x, y), 1, 1)

        if self._paper_mode:
            painter.setPen(solid)
            painter.setBrush(Qt.NoBrush)
            painter.drawLine(0, 0, 0, height)
            painter.drawLine(width - 1, 0, width - 1, height)

    def _rule_line_ys(self, height):
        """Baseline y (viewport coordinates) of every ruled line across the page.

        Anchored to the real text layout via cursorRect, which maps a document
        position to viewport coordinates exactly (accounting for scroll offset and
        document margins), so each line of text sits on a rule with no drift. The
        rhythm is then extended above and below the text to fill the whole page."""
        block = self.first_visible_block()
        if not block.isValid():
            return [], 0
        baselines = []
        pitch = 0
        b = block
        while b.isValid():
            top = self.cursorRect(QTextCursor(b)).top()
            if top > height:
                break
            bl = b.layout()
            for i in range(bl.lineCount()):
                line = bl.lineAt(i)
                baselines.append(int(round(top + line.y() + line.ascent())))
                if pitch == 0:
                    pitch = int(round(line.height()))
            b = b.next()
        if not baselines:
            return [], 0
        if pitch <= 0:
            pitch = max(int(round(self.fontMetrics().height() * 1.5)), 8)
        y = baselines[0] - pitch
        while y >= -pitch:
            baselines.insert(0, y)
            y -= pitch
        y = baselines[-1] + pitch
        while y <= height + pitch:
            baselines.append(y)
            y += pitch
        return baselines, pitch

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._cursor_visible = True
        self._blink_timer.start()
        self.viewport().update(self._block_cursor_rect())
        self.photos.pointer.caret_moved()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        menu = self._slash_menu
        if menu is not None and not menu.underMouse():
            # A click on the menu itself picks; anything else closes it.
            self._close_slash()
        self._cursor_visible = False
        self._blink_timer.stop()
        self.viewport().update(self._block_cursor_rect())
        # A picture being moved is put back; the ring goes.
        self.photos.focus_lost()
        self.photos.pointer.caret_moved()

    def set_markdown_shortcuts_check(self, check) -> None:
        """Install ``check()``: whether Markdown Shortcuts are on."""
        self._markdown_shortcuts = check

    def _shortcuts_apply(self) -> bool:
        return bool(self._markdown_shortcuts()) and self._holds_structure()

    def _after_typed(self, text: str) -> None:
        """After ``text`` was typed at the caret: the slash menu for a "/"
        at the start of a word, a mention for an "@" there while a Nostr
        account is in use, and Markdown Shortcuts: the structure or style
        the text completes, as an undo step of its own."""
        if text == "/":
            self._maybe_open_slash()
        if text == "@":
            self._maybe_ask_mention()
        if not text or not self._shortcuts_apply():
            return
        cursor = self.textCursor()
        # What a shortcut makes is a step of its own, named as the command
        # that makes it would be.
        before = self.document().availableUndoSteps()
        made = markdown_shortcuts.after_space(cursor) if text == " " else ""
        if made:
            self.setTextCursor(cursor)
            level = cursor.block().blockFormat().headingLevel()
            self.setCurrentCharFormat(rich_text.restyled(self.currentCharFormat(), level))
            self._update_active_format()
            self._name_step(before, _SHORTCUT_STEPS.get(made.split()[0], ""))
            return
        style = markdown_shortcuts.after_mark(cursor) if not text[-1].isalnum() else None
        if style:
            # What is typed next is in the style the opening mark was
            # typed in, which the cursor carries.
            self.setTextCursor(cursor)
            self._update_active_format()
            self._name_step(before, _STYLE_STEPS.get(style, ""))

    def inputMethodEvent(self, event):
        # Characters some keyboards compose (a backtick or a tilde after a
        # dead key, the accent menu, Chinese, Japanese or Korean input)
        # arrive here rather than as key presses: inside a mention, they go
        # after it, as typed letters do (composing starts there too).
        if event.commitString() or event.preeditString():
            self._step_out_of_mention()
            cursor = self.textCursor()
            if not cursor.hasSelection() and photo_group.is_group_block(cursor.block()):
                # Among the pictures of a photo group: composed after it,
                # as typed letters go (_keep_groups_whole).
                with self._typing(TYPED, cursor):
                    self._open_paragraph_beside_group(cursor)
        if event.commitString():
            # What they compose is typed: in the run of typing going on.
            with self._typing(TYPED):
                super().inputMethodEvent(event)
        else:
            super().inputMethodEvent(event)
        self._after_typed(event.commitString())

    # -------- The slash menu --------
    def set_slash_items(self, provider) -> None:
        """Install ``provider(query) -> [slash_menu.SlashItem]``: what the
        slash menu offers for the words typed after the slash. Without
        one, "/" is only a character."""
        self._slash_items = provider

    def slash_menu(self):
        """The slash menu while it is open, else None."""
        menu = self._slash_menu
        return menu if self._slash_at >= 0 and menu is not None and menu.isVisible() else None

    def _announce_place(self) -> None:
        """Tell a screen reader what the caret has entered that its words
        do not say: a checklist item ticked or not, a table cell's row,
        column and header, a photo group's picture (its place in the group
        and its description, photo_group_view.picture_words). Once per item,
        cell or picture."""
        if not self._holds_structure():
            return
        cursor = self.textCursor()
        block = cursor.block()
        at = photo_group_view.picture_at_cursor(cursor)
        picture = photo_group_view.picture_words(at)
        if picture is not None:
            place = ("picture", at[0], picture)
            if place != self._spoken_place:
                self._spoken_place = place
                if not self.photos.quiet:
                    # (A command that put the caret there says what it did.)
                    announce.announce(self, picture)
            return
        table = cursor.currentTable()
        if table is not None:
            cell = table.cellAt(cursor)
            place = ("cell", table.firstPosition(), cell.row(), cell.column())
            if place != self._spoken_place:
                self._spoken_place = place
                header = ""
                if cell.row() > 0:
                    header = rich_text.cell_text(table.cellAt(0, cell.column()))
                said = _("Row {row} of {rows}, column {column}").format(
                    row=cell.row() + 1, rows=table.rows(), column=cell.column() + 1)
                if header:
                    said = _("{place}, {header}").format(place=said, header=header)
                announce.announce(self, said)
            return
        if rich_text.is_task(block):
            place = ("task", block.blockNumber(), block.blockFormat().marker())
            if place != self._spoken_place:
                self._spoken_place = place
                announce.announce(self, _("checked")
                                  if block.blockFormat().marker() == rich_text.TICKED
                                  else _("not checked"))
            return
        self._spoken_place = None

    @_named_step(N_("Checklist"))
    def toggle_ticks(self) -> None:
        """Format > Mark as Checked: tick the checklist items under the
        caret, or untick them when all are ticked, and say which."""
        cursor = self.textCursor()
        ticked = rich_text.toggle_ticks(cursor)
        if ticked is None:
            return
        self._spoken_place = None
        announce.announce(self, _("checked") if ticked else _("not checked"))
        self.viewport().update()

    def _maybe_ask_mention(self) -> None:
        """An "@" was just typed: ask for a person to mention when it starts
        a word (not in an email address), in a document that holds Markdown
        structure, outside code, while a Nostr account is in use."""
        if not self._nostr_active() or not self._holds_structure():
            return
        cursor = self.textCursor()
        block = cursor.block()
        if rich_text.is_code_block(block) or rich_text.holds_link_or_code(cursor):
            return
        text = block.text()
        before = text[:text_index(text, cursor.positionInBlock()) - 1]
        if before and not (before[-1].isspace() or before[-1] in "([{\"'\u201c\u2018"):
            return
        self.mention_requested.emit(cursor.position() - 1)

    @_named_step(N_("Mention"))
    def insert_mention(self, at: int, href: str, name: str) -> None:
        """The mention of ``name`` (``href``, a person's nostr: link) in place
        of the "@" typed at ``at`` (when it is still there), else at the
        caret. One undo step."""
        cursor = self.textCursor()
        doc = self.document()
        if 0 <= at < doc.characterCount() and doc.characterAt(at) == "@":
            cursor.setPosition(at)
            cursor.setPosition(at + 1, QTextCursor.MoveMode.KeepAnchor)
        rich_text.insert_mention(cursor, href, name)
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(rich_text.without_link(self.currentCharFormat()))

    def _maybe_open_slash(self) -> None:
        """A "/" was just typed: the slash menu, at the start of a word in a
        paragraph that can take structure (not in code or a table)."""
        if self._slash_items is None or not self._holds_structure():
            return
        cursor = self.textCursor()
        block = cursor.block()
        if rich_text.is_code_block(block) or cursor.currentTable() is not None:
            return
        text = block.text()
        before = text[:text_index(text, cursor.positionInBlock()) - 1]
        if before and not before[-1].isspace():
            return
        self._slash_at = cursor.position() - 1
        self._refresh_slash()

    def _slash_query(self):
        """The words typed after the slash, or None when the caret has
        left them or the slash is gone."""
        at = self._slash_at
        cursor = self.textCursor()
        doc = self.document()
        if (at < 0 or cursor.hasSelection() or cursor.position() <= at
                or doc.findBlock(at).position() != cursor.block().position()
                or doc.characterAt(at) != "/"):
            return None
        words = QTextCursor(doc)
        words.setPosition(at + 1)
        words.setPosition(cursor.position(), QTextCursor.MoveMode.KeepAnchor)
        return words.selectedText()

    def _refresh_slash(self) -> None:
        query = self._slash_query()
        if query is None or query[:1].isspace():
            # Gone, or a space right after the slash: a slash in a sentence
            # ("either / or", "km / h"), not a command.
            self._close_slash()
            return
        items = self._slash_items(query)
        if not items and (len(query) >= 3 or query.endswith(" ")):
            self._close_slash()
            return
        if self._slash_menu is None:
            self._slash_menu = SlashMenu(self)
            self._slash_menu.picked.connect(self._pick_slash)
        menu = self._slash_menu
        opening = not menu.isVisible()
        if opening or query != self._slash_shown:
            menu.show_items(items, headers=not query, dark=self._is_dark())
            self._slash_shown = query
            self._slash_moved = False
        if not menu.has_choices():
            # Nothing matches yet: out of the way, while the words may
            # still change into something that does.
            menu.hide()
            return
        menu.open_at(self.cursorRect(), self.viewport())
        self._announce_slash_choice(opening=opening)

    def _slash_follow_caret(self) -> None:
        if self._slash_at >= 0:
            self._refresh_slash()

    def _close_slash(self) -> None:
        self._slash_at = -1
        self._slash_shown = None
        self._slash_said = ""
        if self._slash_menu is not None:
            self._slash_menu.hide()
        if self.accessibleDescription():
            self.setAccessibleDescription("")

    def _announce_slash_choice(self, *, opening: bool = False) -> None:
        """Screen readers hear the chosen item, and that the menu opened:
        the menu never has the focus, so the editor announces it (and
        describes it, for a reader that asks)."""
        menu = self._slash_menu
        title = menu.chosen_title() if menu is not None else ""
        self.setAccessibleDescription(title)
        if not title:
            return
        position, count = menu.choice_position()
        choice = _("{title}, {position} of {count}").format(
            title=_bare_title(title), position=position, count=count)
        if choice == self._slash_said and not opening:
            return                            # heard already
        self._slash_said = choice
        announce.announce(self, _("Insert menu. {choice}").format(choice=choice)
                          if opening else choice)

    def _pick_slash(self, action) -> None:
        """Take "/words" away and run ``action`` where the slash was, as one
        step to undo."""
        start = self._slash_at
        end = self.textCursor().position()
        self._close_slash()
        if start < 0:
            return
        title = _bare_title(action.text().replace("&", ""))
        if not action.text().endswith("\u2026"):
            # (One that asks first, "Add Photos…", says nothing done yet.)
            announce.announce(self, _("{title} applied").format(title=title))
        # One step, called by the command's own name.
        with self.undo_step(title):
            words = QTextCursor(self.document())
            words.setPosition(start)
            words.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            words.removeSelectedText()
            self.setTextCursor(words)
            # The text has the focus all along (the menu never takes it); a
            # command may give it to a panel of its own (Add Link's).
            action.trigger()

    def _slash_key(self, e) -> bool:
        """The keys of the open slash menu. True when one was handled."""
        menu = self.slash_menu()
        if menu is None:
            return False
        key = e.key()
        plain = e.modifiers() in (Qt.KeyboardModifier.NoModifier,
                                  Qt.KeyboardModifier.KeypadModifier)
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and plain:
            menu.move_choice(-1 if key == Qt.Key.Key_Up else 1)
            self._slash_moved = True
            self._announce_slash_choice()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab) and plain:
            action = menu.chosen()
            if action is not None and (self._slash_query() or self._slash_moved):
                self._pick_slash(action)
                return True
            # A bare slash ("the root is /"): the key does what it does.
            self._close_slash()
            return False
        if key == Qt.Key.Key_Escape:
            self._close_slash()
            return True
        return False

    def set_paste_guard(self, guard) -> None:
        """Install ``guard(text) -> text, other text or None``, asked with
        the words of every paste and drop: the same text pastes as it
        came, other text is pasted in its place (as plain text), None
        pastes nothing (a secret key, see MainWindow._guard_paste)."""
        self._paste_guard = guard

    def set_local_formats_check(self, check) -> None:
        """Install ``check()``: whether this document's file keeps what
        Markdown has no words for (underline, colors), as HTML does. A copy
        made in this editor then pastes back with them."""
        self._keeps_local_formats = check

    def set_pictures_check(self, check) -> None:
        """Install ``check()``: whether this document's file keeps a
        picture. Where it does not (plain text, code), a picture on the
        clipboard is not taken, as a plain-text editor takes none."""
        self._keeps_pictures = check

    def set_structure_check(self, check) -> None:
        """Install ``check()``: whether this document holds Markdown
        structure. Where it does not (a .txt file, code, Markdown opened
        as its text), Tab keeps typing bullets as text."""
        self._holds_structure = check

    # -------- Lists --------
    @_named_step(N_("Checklist"))
    def toggle_checklist(self) -> None:
        """Checklist for the paragraphs under the cursor; again, the boxes
        go and they are plain list items."""
        cursor = self.textCursor()
        rich_text.toggle_checklist(cursor)
        self.setTextCursor(cursor)

    @_named_step(N_("List"))
    def toggle_list(self, kind: str) -> None:
        """Bulleted (rich_text.BULLET) or Numbered List (rich_text.NUMBER)
        for the paragraphs under the cursor; again, it is taken away."""
        cursor = self.textCursor()
        rich_text.toggle_list(cursor, kind)
        self.setTextCursor(cursor)

    def change_indent(self, delta: int) -> None:
        """Increase (+1) or Decrease (-1) Indent: list nesting, or the
        indent of a bullet typed as text."""
        cursor = self.textCursor()
        with self.undo_step(_INDENT_STEPS[delta]):
            indented = rich_text.change_indent(cursor, delta)
        if indented:
            self.setTextCursor(cursor)
            return
        line, start = self._line_info(cursor)
        spaces, has_bullet = self._indent_level_and_has_bullet(line)
        if has_bullet:
            self._indent_typed_bullet(line, start, spaces, has_bullet, delta)

    # -------- Audio and video --------
    def set_object_lookup(self, lookup) -> None:
        """Install ``lookup(name)``: for one of the app's own objects (a
        picture, audio or video) a paste.Outside, how other apps get it on
        the clipboard, and whether it is audio or video (a paragraph of its
        own, kept that way); None for anything else."""
        self._outside = lookup

    def _outside_of(self, name: str):
        return self._outside(name) if self._outside is not None else None

    def _is_media(self, name: str) -> bool:
        found = self._outside_of(name)
        return found is not None and found.media

    def is_media_block(self, block) -> bool:
        """Whether ``block`` holds an audio or video object and nothing else."""
        if self._outside is None or not block.isValid() or block.text() != "\ufffc":
            return False
        fmt = block.begin().fragment().charFormat()
        return fmt.isImageFormat() and self._is_media(fmt.toImageFormat().name())

    def _keep_media_apart(self, e) -> bool:
        """Audio or video stays a paragraph of its own: typing in its
        paragraph goes into a new one after it (before it, with the caret
        before it), as one run of typing with the words; Return there starts
        a clean paragraph; and Backspace from the paragraph below (or Delete
        from the one above) takes it away whole, as one step, rather than
        joining text to it. True when the key was answered here."""
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        block = cursor.block()
        typing = bool(e.text()) and e.text().isprintable()
        if (e.key() in (Qt.Key_Return, Qt.Key_Enter) or typing) \
                and self.is_media_block(block) and cursor.atBlockStart():
            # Before it, as with any block: a clean paragraph above it, the
            # caret staying before it on Return and going into it to type.
            if typing:
                # The paragraph and the words typed into it: one run.
                with self._typing(TYPED, cursor):
                    _open_paragraph_above(cursor)
                return False                   # the character is typed there
            with self._typing(TYPED, cursor):
                _open_paragraph_above(cursor)
                cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
            return True
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.is_media_block(block):
            # A clean paragraph (one Qt makes would take the object's
            # format), in the run of typing.
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self.setTextCursor(cursor)
            with self._typing(TYPED, cursor):
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            return True
        if e.text() and e.text().isprintable() and self.is_media_block(block):
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self.setTextCursor(cursor)
            # The paragraph and the words typed into it: one run.
            with self._typing(TYPED, cursor):
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            return False                       # the character is typed there
        if e.key() == Qt.Key_Backspace and cursor.atBlockStart() \
                and self.is_media_block(block.previous()):
            self._remove_media_block(block.previous(), BACKWARD)
            return True
        if e.key() == Qt.Key_Delete and cursor.atBlockEnd() \
                and self.is_media_block(block.next()) and not self.is_media_block(block):
            self._remove_media_block(block.next(), FORWARD)
            return True
        return False

    def _keep_groups_whole(self, e) -> bool:
        """A photo group holds pictures and nothing else. What is typed
        with the caret among its pictures (or Return there) goes into a
        new paragraph after the group, before it with the caret before its
        first picture, as one run of typing with the words. Backspace
        from the words below a group, or Delete from the words above it,
        goes to the group's edge and takes nothing away: the next one
        takes a picture. (Backspace and Delete among the pictures are
        Qt's own: one picture each.) True when the key was answered here."""
        cursor = self.textCursor()
        block = cursor.block()
        in_group = photo_group.is_group_block(block)
        plain = e.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier,
                                  Qt.KeyboardModifier.ShiftModifier)
        returns = e.key() in (Qt.Key_Return, Qt.Key_Enter)
        typing = bool(e.text()) and e.text().isprintable() and not e.modifiers() & (
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)
        if in_group and (returns or typing):
            if cursor.hasSelection() and self.document().findBlock(cursor.anchor()) != block:
                return False            # a selection that reaches out of the group
            with self._typing(TYPED, cursor):
                if cursor.hasSelection():
                    cursor.removeSelectedText()
                self._open_paragraph_beside_group(cursor, stay=returns)
            return returns              # a character is typed there
        if in_group and e.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            return True                 # nothing to indent among pictures
        if cursor.hasSelection() or not plain:
            return False
        # From a paragraph with something in it: to the edge, nothing lost.
        if e.key() == Qt.Key_Backspace and cursor.atBlockStart():
            before = block.previous()
            if before.isValid() and (before.length() > 1 if in_group else (
                    block.length() > 1 and photo_group.is_group_block(before))):
                cursor.setPosition(before.position() + before.length() - 1)
                self.setTextCursor(cursor)
                return True
        if e.key() == Qt.Key_Delete and cursor.atBlockEnd():
            after = block.next()
            if after.isValid() and (after.length() > 1 if in_group else (
                    block.length() > 1 and photo_group.is_group_block(after))):
                cursor.setPosition(after.position())
                self.setTextCursor(cursor)
                return True
        return False

    @staticmethod
    def _open_paragraph_beside_group(cursor: QTextCursor, *, stay: bool = False) -> None:
        """A clean paragraph after the photo group ``cursor`` is in, the
        cursor in it; with the cursor before the group's first picture,
        above the group (and with ``stay``, Return there, the cursor
        stays before that picture)."""
        block = cursor.block()
        own = photo_group.own_format(block)
        if block.length() == 1:
            cursor.setBlockFormat(own)             # its pictures all gone
        elif cursor.atBlockStart():
            cursor.insertBlock(block.blockFormat(), block.charFormat())
            cursor.movePosition(QTextCursor.MoveOperation.PreviousBlock)
            cursor.setBlockFormat(own)
            cursor.setBlockCharFormat(QTextCharFormat())
            if stay:
                cursor.movePosition(QTextCursor.MoveOperation.NextBlock)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            cursor.insertBlock(own, QTextCharFormat())

    def _remove_media_block(self, block, kind: str) -> None:
        """Take the paragraph of audio or video ``block`` away, in the run
        of Backspace (``kind`` BACKWARD) or of Delete (FORWARD)."""
        with self._typing(kind):
            cursor = QTextCursor(block)
            cursor.movePosition(QTextCursor.MoveOperation.NextBlock,
                                QTextCursor.MoveMode.KeepAnchor)
            if cursor.position() == block.position():          # the last paragraph
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                    QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

    # -------- Links --------
    def set_nostr_check(self, check) -> None:
        """Install ``check()``: whether a Nostr account is in use."""
        self._nostr_active = check

    def nostr_in_use(self) -> bool:
        return bool(self._nostr_active())

    def set_library_check(self, check) -> None:
        """Install ``check(address)``: whether a web address names a file
        of the Media Library."""
        self._in_library = check

    def set_media_badges(self, badge_of) -> None:
        """Install ``badge_of(name)``: the media_badge.Badge the object of
        the text named ``name`` wears, or None."""
        self._media_badge = badge_of

    def set_drop_check(self, check) -> None:
        """Install ``check(urls)``: whether dropping those files brings
        anything to open or put in. Otherwise the pointer shows the drop
        is not accepted, before it is let go."""
        self._drop_check = check

    def set_link_opener(self, opener) -> None:
        """Install ``opener(href)``, which opens a Command-clicked link."""
        self._link_opener = opener

    def link_at_caret(self):
        """``(start, end, href)`` of the link at the caret, or None."""
        return rich_text.link_range(self.textCursor())

    def show_link_popover(self) -> None:
        """Add Link, or Edit Link when the caret is in one: the popover
        under the words, prefilled with what is known."""
        cursor = self.textCursor()
        found = rich_text.link_range(cursor)
        if found is not None:
            start, end, href = found
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            text = cursor.selectedText()
        else:
            href = clipboard_address()
            cursor = self._trimmed_selection(cursor)
            self.setTextCursor(cursor)
            text = cursor.selectedText()
        several = "\u2029" in text
        popover = LinkPopover(text=text.replace("\u2029", " ").replace("\u2028", " "),
                              href=href, editing=found is not None,
                              nostr=self._nostr_active(), text_editable=not several,
                              parent=self)
        popover.applied.connect(self.apply_link)
        popover.removed.connect(self.remove_link)
        # Escape, Cancel or a click elsewhere: the writing goes on here.
        # (A method, not a lambda: Qt drops the connection if the editor
        # goes first, a lambda would call into a deleted editor.)
        popover.destroyed.connect(self.take_focus_back)
        popover.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        start_rect = self.cursorRect(self._selection_start_cursor())
        popover.show_below(start_rect, self.viewport())

    def take_focus_back(self, _popover=None) -> None:
        """A panel at the caret closed: the writing goes on here (its
        window active again, as the panel had it)."""
        self.window().activateWindow()
        self.setFocus()

    @_named_step(N_("Note or Article"))
    def insert_reference(self, uri: str) -> None:
        """The reference ``uri`` (``nostr:nevent1…``, ``nostr:naddr1…``) in
        a paragraph of its own, as written: readers show it as a card only
        when it stands alone, and it reads back the same from Markdown."""
        cursor = self.textCursor()
        rich_text.insert_own_paragraph(cursor, uri)
        self.setTextCursor(cursor)

    @staticmethod
    def _trimmed_selection(cursor: QTextCursor) -> QTextCursor:
        """The selection without the spaces (and paragraph ends) at its
        edges: they stay outside the link, as words processors keep them,
        instead of being deleted with it."""
        selected = cursor.selectedText()
        if not selected.strip():
            return cursor
        lead = len(selected) - len(selected.lstrip())
        trail = len(selected) - len(selected.rstrip())
        trimmed = QTextCursor(cursor)
        trimmed.setPosition(cursor.selectionStart() + lead)
        trimmed.setPosition(cursor.selectionEnd() - trail, QTextCursor.MoveMode.KeepAnchor)
        return trimmed

    def _selection_start_cursor(self) -> QTextCursor:
        cursor = QTextCursor(self.textCursor())
        cursor.setPosition(cursor.selectionStart())
        return cursor

    @_named_step(N_("Link"))
    def apply_link(self, text: str, href: str) -> None:
        """Link the selection to ``href``: its words as they are, or
        ``text`` in their place when the person retyped them. With nothing
        selected, ``text`` (or else the address itself) is inserted."""
        cursor = self.textCursor()
        if not cursor.hasSelection() and not text:
            text = link_url.display_href(href, limit=10_000)
        rich_text.set_link(cursor, text, href)
        cursor.setPosition(cursor.selectionEnd())
        self.setTextCursor(cursor)
        self._leave_link_at_end()
        self.setFocus()

    @_named_step(N_("Remove Link"))
    def remove_link(self) -> None:
        cursor = self.textCursor()
        rich_text.remove_link(cursor)
        self.setTextCursor(cursor)

    def _open_link(self, href: str) -> None:
        if self._link_opener is not None:
            self._link_opener(href)
            return
        web = link_url.web_address_for(href)
        mail = link_url.mail_address_for(href)
        if web and url_safety.is_safe_external_url(web):
            QDesktopServices.openUrl(QUrl(web))
        elif mail:
            QDesktopServices.openUrl(QUrl(mail))

    def _step_out_of_mention(self) -> None:
        """A mention is one name: what is typed or pasted with the caret
        inside it goes after it, as in Slack and Notion. (Words inside it
        would be part of the link, which is written as the bare address.)"""
        cursor = self.textCursor()
        mention = rich_text.mention_at(cursor) if self._holds_structure() else None
        if mention is not None and mention[0] < cursor.position() < mention[1]:
            cursor.setPosition(mention[1])
            self.setTextCursor(cursor)

    def _remove_mention(self, start: int, end: int, kind: str) -> None:
        """A mention goes as a whole (Backspace, Delete): half a name links
        to nobody. In the run of Backspace (``kind`` BACKWARD) or of Delete
        (FORWARD)."""
        with self._typing(kind) as cursor:
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
        self.ensureCursorVisible()

    def _leave_link_at_end(self) -> None:
        """Typing right before or right after a link is not part of it
        (links are not "sticky", as in every word processor); typing
        inside one is."""
        cursor = self.textCursor()
        fmt = self.currentCharFormat()
        if cursor.hasSelection() or not fmt.isAnchor():
            return
        if not cursor.atBlockStart() and not cursor.atBlockEnd():
            after = QTextCursor(cursor)
            after.movePosition(QTextCursor.MoveOperation.NextCharacter)
            before, following = cursor.charFormat(), after.charFormat()
            if (before.isAnchor() and following.isAnchor()
                    and before.anchorHref() == following.anchorHref()):
                return              # inside the link
        self.setCurrentCharFormat(rich_text.without_link(fmt))

    def _media_hit(self, point):
        """The audio or video object at ``point`` (in the viewport) and the
        point within it, or None. Such an object is a paragraph of its own
        (is_media_block), so it is the first thing on its line."""
        block = self.cursorForPosition(point).block()
        if not self.is_media_block(block):
            return None
        fmt = block.begin().fragment().charFormat().toImageFormat()
        layout = block.layout()
        line = layout.lineAt(0)
        if not line.isValid():
            return None
        left = layout.position().x() + line.x() - self.horizontalScrollBar().value()
        top = (layout.position().y() + line.y() + line.ascent() - fmt.height()
               - self.verticalScrollBar().value())
        inside = QPointF(point) - QPointF(left, top)
        if not (0 <= inside.x() <= fmt.width() and 0 <= inside.y() <= fmt.height()):
            return None
        return fmt.name(), inside

    def mouseDoubleClickEvent(self, event):
        # Audio or video double-clicked plays, as a file does in the Finder.
        hit = (self._media_hit(event.position().toPoint())
               if event.button() == Qt.MouseButton.LeftButton else None)
        if hit is not None:
            self.media_clicked.emit(hit[0], hit[1], True)
            event.accept()
            return
        # A picture of a photo group double-clicked is selected and shown
        # in Quick Look, as a file is in the Finder; Space puts it away.
        picture = (self._group_picture_hit(event.position().toPoint())
                   if event.button() == Qt.MouseButton.LeftButton else None)
        if picture is not None:
            position, name = picture
            cursor = self.textCursor()
            cursor.setPosition(position)
            cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self.picture_look_requested.emit(name)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _group_picture_hit(self, point):
        """The group picture under ``point`` (the viewport's), or None."""
        at = QPointF(point) + QPointF(self.horizontalScrollBar().value(),
                                      self.verticalScrollBar().value())
        return photo_group_view.picture_at(self.document(), at)

    def mousePressEvent(self, event):
        self._pressed_at = event.position().toPoint()
        # Command-click (Ctrl-click on Windows and Linux) opens a link, and
        # goes from a footnote's mark to its note and back.
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            href = self.anchorAt(event.position().toPoint())
            if href:
                self._open_link(href)
                event.accept()
                return
            if self._holds_structure() and self.jump_to_footnote(
                    self.cursorForPosition(event.position().toPoint())):
                event.accept()
                return
        # A picture pressed: selected, a chip clicked, or a drag begun.
        if self.photos.pointer.mouse_press(event):
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Chips on the picture under the pointer; a picture dragged.
        if self.photos.pointer.mouse_move(event):
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.photos.pointer.mouse_release(event):
            event.accept()
            return
        # A checklist item's box clicked: Qt ticks it (or unticks it), as
        # Mark as Checked would, and the step is named as that command's.
        before = self.document().availableUndoSteps()
        super().mouseReleaseEvent(event)
        if self.document().availableUndoSteps() != before and rich_text.is_task(
                self.cursorForPosition(event.position().toPoint()).block()):
            self._name_step(before, N_("Checklist"))
        # A click (not the end of a drag) on audio or video.
        point = event.position().toPoint()
        pressed = getattr(self, "_pressed_at", None)
        if (event.button() == Qt.MouseButton.LeftButton and pressed is not None
                and (point - pressed).manhattanLength() < QApplication.startDragDistance()):
            hit = self._media_hit(point)
            if hit is not None:
                self.media_clicked.emit(hit[0], hit[1], False)
            picture = self._group_picture_hit(point)
            if picture is not None and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                # A picture of a photo group clicked is selected, as a
                # picture is in Pages: Space then shows it, Backspace
                # takes it away.
                cursor = self.textCursor()
                cursor.setPosition(picture[0])
                cursor.setPosition(picture[0] + 1, QTextCursor.MoveMode.KeepAnchor)
                self.setTextCursor(cursor)

    def jump_to_footnote(self, at: QTextCursor) -> bool:
        """From a footnote's mark to the words of its note, or from a
        note's label to where it is marked first. True when it went."""
        target = rich_text.footnote_jump(at)
        if target is None:
            return False
        cursor = self.textCursor()
        cursor.setPosition(target)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        return True

    @_named_step(N_("Footnote"))
    def insert_footnote(self) -> None:
        """Insert > Footnote: the next number's mark at the caret, and its
        note at the end of the document, where the caret goes."""
        cursor = self.textCursor()
        if rich_text.insert_footnote(cursor):
            self.setTextCursor(cursor)
            self.setCurrentCharFormat(QTextCharFormat())
            self.ensureCursorVisible()

    def _footnote_tip(self, point) -> str:
        """The note of the footnote mark at ``point`` ("" for none)."""
        if not self._holds_structure():
            return ""
        cursor = self.cursorForPosition(point)
        block = cursor.block()
        if rich_text.is_code_block(block):
            return ""
        text = block.text()
        offset = text_index(text, cursor.positionInBlock())
        for found in rich_text.FOOTNOTE_MARK.finditer(text):
            if found.start() <= offset <= found.end():
                return rich_text.footnote_text(self.document(), found.group(1))
        return ""

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.photos.pointer.leave()
        if event.type() == QEvent.Type.ToolTip:
            chip = self.photos.pointer.tooltip(event.pos())
            if chip:
                QToolTip.showText(event.globalPos(), chip, self)
                return True
            href = self.anchorAt(event.pos())
            note = "" if href else self._footnote_tip(event.pos())
            badge = None if href or note else self._badge_at(event.pos())
            if badge is not None:
                QToolTip.showText(event.globalPos(), badge.hint, self)
            elif href:
                how = (_("Command-click to open") if sys.platform == "darwin"
                       else _("Ctrl+click to open"))
                QToolTip.showText(event.globalPos(),
                                  link_url.display_href(href) + "\n" + how, self)
            elif note:
                how = (_("Command-click to go to the note") if sys.platform == "darwin"
                       else _("Ctrl+click to go to the note"))
                QToolTip.showText(event.globalPos(), note + "\n" + how, self)
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(event)

    def media_object_at(self, pos):
        """The name of the picture, audio or video under ``pos`` (viewport
        coordinates), or None."""
        return media_badge.object_at(self, pos)

    def _badge_at(self, pos):
        name = self.media_object_at(pos)
        badge = self._media_badge(name) if name else None
        return badge if badge is not None and badge.hint else None

    # -------- Quotes and dividers --------
    @_named_step(N_("Quote"))
    def toggle_quote(self) -> None:
        """Quote the paragraphs under the cursor, or take the quote away."""
        cursor = self.textCursor()
        rich_text.toggle_quote(cursor)
        self.setTextCursor(cursor)
        self.viewport().update()

    @_named_step(N_("Divider"))
    def insert_divider(self) -> None:
        """A divider after the paragraph at the cursor; typing goes on below it."""
        cursor = self.textCursor()
        rich_text.insert_divider(cursor)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    # -------- Context menu --------
    def set_context_menu_filler(self, filler) -> None:
        """Install ``filler(menu, editor, pos)``, which puts the commands in
        the context menu (``pos`` is where it was asked for, in viewport
        coordinates). Injected, so the editor holds no command list."""
        self._context_menu_filler = filler

    def contextMenuEvent(self, event):
        if event.reason() == event.Reason.Mouse:
            # A picture right-clicked is selected: the menu is about it.
            self.photos.context_at(event.pos())
        menu = QMenu(self)
        if self._is_dark():
            menu.setStyleSheet("""
                QMenu {
                    background: #252526;
                    color: #CCCCCC;
                    border: 1px solid #3C3C3C;
                    padding: 4px;
                }
                QMenu::item { padding: 4px 20px 4px 30px; }
                QMenu::item:selected { background: #1E1E1E; color: #FFFFFF; }
                QMenu::item:disabled { color: #6A6A6A; }
                QMenu::separator { height: 1px; background: #3C3C3C; margin: 4px 0px; }
            """)
        else:
            menu.setStyleSheet("""
                QMenu {
                    background: #F8F8F8;
                    color: #333333;
                    border: 1px solid #E1E1E1;
                    padding: 4px;
                }
                QMenu::item { padding: 4px 20px 4px 30px; }
                QMenu::item:selected { background: #F3F3F3; color: #000000; }
                QMenu::item:disabled { color: #999999; }
                QMenu::separator { height: 1px; background: #E1E1E1; margin: 4px 0px; }
            """)
        if self._context_menu_filler is not None:
            self._context_menu_filler(menu, self, event.pos())
        else:
            menu.addAction(_("Cut"), self.cut)
            menu.addAction(_("Copy"), self.copy)
            menu.addAction(_("Paste"), self.paste_from_clipboard)
        menu.exec(event.globalPos())
        # Built for this one click: gone with it (the window's shared Color
        # and Style menus in it are not its children, and stay).
        menu.deleteLater()

    # -------- Bullet Logic (•) --------
    @staticmethod
    def _line_info(cursor: QTextCursor):
        """Get current line text + start position of the line."""
        c = QTextCursor(cursor)
        c.movePosition(QTextCursor.StartOfLine, QTextCursor.MoveAnchor)
        start_pos = c.position()
        c.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
        line_text = c.selectedText()
        return line_text, start_pos

    def _indent_level_and_has_bullet(self, line: str):
        spaces = 0
        i = 0
        while i < len(line) and line[i] == ' ':
            spaces += 1
            i += 1
        has_bullet = line[i:i+2] == "• "
        return spaces, has_bullet

    def _set_line_text(self, start_pos: int, new_text: str, cursor_pos_after=None):
        c = self.textCursor()
        c.setPosition(start_pos)
        c.movePosition(QTextCursor.EndOfLine, QTextCursor.KeepAnchor)
        c.beginEditBlock()
        c.removeSelectedText()
        c.insertText(new_text)
        c.endEditBlock()

        if cursor_pos_after is None:
            cursor_pos_after = start_pos + doc_length(new_text)

        nc = self.textCursor()
        nc.setPosition(cursor_pos_after)
        self.setTextCursor(nc)

    def _indent_typed_bullet(self, line: str, start: int, spaces: int, has_bullet: bool,
                             delta: int) -> None:
        """Tab and Shift+Tab on a line with a bullet typed as text ("• "):
        four spaces more or less, and a bullet for a line that has none."""
        tab_width = 4
        if delta > 0:
            new_line = " " * (spaces + tab_width)
            if not has_bullet:
                new_line += "• " + line.lstrip()
            else:
                new_line += line[spaces:]
        else:
            new_line = " " * max(0, spaces - tab_width) + line[spaces:]
        bullet_pos = new_line.find("• ")
        # (Places in the line as places in the document: an emoji takes two.)
        move_to = (start + doc_offset(new_line, bullet_pos + 2) if bullet_pos >= 0
                   else start + doc_length(new_line.rstrip()))
        with self.undo_step(_INDENT_STEPS[delta]):
            self._set_line_text(start, new_line, move_to)
        if delta > 0:
            self._apply_active_format_to_cursor()

    def keyPressEvent(self, e):
        if self._slash_key(e) or self.photos.keys.key_press(e):
            return
        self._key_press(e)
        if self._slash_at >= 0:
            # The words after the slash changed (or the caret left them).
            self._refresh_slash()

    def _key_press(self, e):
        # Space with the caret at a picture of a photo group (or the
        # picture selected) shows it in Quick Look, or puts Quick Look
        # away, as in the Finder; so does Escape.
        if e.modifiers() in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.KeypadModifier):
            if e.key() == Qt.Key.Key_Space:
                picture = photo_group_view.picture_at_cursor(self.textCursor())
                if picture is not None:
                    self.picture_look_requested.emit(
                        "" if self._picture_look_open() else picture[3].name)
                    return
            if e.key() == Qt.Key.Key_Escape and self._picture_look_open():
                self.picture_look_requested.emit("")
                return
        # Keep cursor solid immediately after a keypress; blink restarts from now
        old_cursor_rect = self._block_cursor_rect()
        self._cursor_visible = True
        self._blink_timer.start()
        self.viewport().update(old_cursor_rect)

        # Undo / Redo
        if e.key() == Qt.Key_Z and e.modifiers() == Qt.ControlModifier:
            self.undo()
            return
        if (e.key() == Qt.Key_Z and e.modifiers() == (Qt.ControlModifier | Qt.ShiftModifier)) or \
           (e.key() == Qt.Key_Y and e.modifiers() == Qt.ControlModifier):
            self.redo()
            return

        reach = self._deletion_reach(e)
        if reach is not None:
            cursor = self.textCursor()
            backward = e.key() == Qt.Key_Backspace
            if cursor.hasSelection() or (cursor.atBlockStart() if backward
                                         else cursor.atBlockEnd()) \
                    or photo_group.is_group_block(cursor.block()):
                # With words selected, at a paragraph's edge or among the
                # pictures of a photo group, it does what Backspace or
                # Delete does there (out of a list, a divider taken away,
                # two paragraphs joined, one picture taken away).
                e = QKeyEvent(e.type(), e.key(), Qt.KeyboardModifier.NoModifier, e.text())
            else:
                self._delete_reach(BACKWARD if backward else FORWARD, reach)
                self.ensureCursorVisible()
                return

        if self._keep_media_apart(e) or self._keep_groups_whole(e):
            self.ensureCursorVisible()
            return

        if e.text() and e.text().isprintable():
            self._step_out_of_mention()
            # The caret may have arrived at a link's edge without moving
            # (a document loaded around it): what is typed is not the link.
            self._leave_link_at_end()

        if self._structure_key(e):
            self.ensureCursorVisible()
            return

        c = self.textCursor()
        line, start = self._line_info(c)
        spaces, has_bullet = self._indent_level_and_has_bullet(line)

        if e.key() == Qt.Key_Tab:
            if c.atBlockStart() or (c.positionInBlock() <= spaces + (2 if has_bullet else 0)):
                self._indent_typed_bullet(line, start, spaces, has_bullet, +1)
                self.ensureCursorVisible()
                return

        elif e.key() == Qt.Key_Backtab:
            if c.atBlockStart() or (c.positionInBlock() <= spaces + (2 if has_bullet else 0)):
                self._indent_typed_bullet(line, start, spaces, has_bullet, -1)
                self.ensureCursorVisible()
                return

        elif e.key() in (Qt.Key_Return, Qt.Key_Enter):
            if has_bullet:
                content_after_bullet = line[spaces + 2:].strip()
                cursor_pos_in_block = c.positionInBlock()
                bullet_end_pos = spaces + 2
                is_empty_bullet = (content_after_bullet == "") and (cursor_pos_in_block <= bullet_end_pos)

                if is_empty_bullet:
                    # Double Enter: remove bullet, start plain line (in the
                    # run of typing)
                    with self._typing(TYPED):
                        c.movePosition(QTextCursor.StartOfBlock)
                        c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                        c.removeSelectedText()
                        c.insertText("\n")
                        c = self.textCursor()
                        c.movePosition(QTextCursor.StartOfBlock)
                        fmt = QTextCharFormat()
                        fmt.setFontWeight(400)
                        fmt.setFontItalic(False)
                        fmt.setFontUnderline(False)
                        fmt.clearForeground()
                        c.setCharFormat(fmt)
                        self.mergeCurrentCharFormat(fmt)
                    self.active_format.update({'bold': False, 'italic': False, 'underline': False, 'color': None})
                    self.setTextCursor(c)
                    self.ensureCursorVisible()
                    return
                else:
                    # Continue bullet on next line (in the run of typing)
                    with self._typing(TYPED, c):
                        c.insertText("\n" + " " * spaces + "• ")
                    self._apply_active_format_to_cursor()
                    self.ensureCursorVisible()
                    return
            else:
                before = self.document().availableUndoSteps()
                if self._shortcuts_apply() and markdown_shortcuts.on_return(c):
                    # "---" and Return: a divider; "```python": a code block
                    # (Markdown Shortcuts), named as those commands are.
                    self._name_step(before, N_("Code Block")
                                    if rich_text.is_code_block(c.block()) else N_("Divider"))
                    self.setTextCursor(c)
                    self.setCurrentCharFormat(c.block().charFormat())
                    self.ensureCursorVisible()
                    return
                if e.modifiers() in (Qt.NoModifier, Qt.KeypadModifier):
                    # A new paragraph in the run of typing it belongs to.
                    with self._typing(TYPED) as cursor:
                        cursor.insertBlock()
                else:
                    # Shift-Return: a line break, in the run of typing too.
                    with self._typing(TYPED):
                        super().keyPressEvent(e)
                self._apply_active_format_to_cursor()
                self.ensureCursorVisible()
                return

        elif e.key() == Qt.Key_Backspace:
            mention = rich_text.mention_at(c) if self._holds_structure() else None
            if (mention is not None and mention[0] < c.position() <= mention[1]
                    and e.modifiers() in (Qt.NoModifier, Qt.KeypadModifier)):
                self._remove_mention(*mention, BACKWARD)
                return
            if not c.hasSelection() and c.positionInBlock() <= spaces + (2 if has_bullet else 0) and (spaces > 0 or has_bullet):
                # In a typed bullet or its indent: one step of typing.
                with self.undo_step(N_("Typing")):
                    if has_bullet:
                        if spaces == 0 and c.blockNumber() > 0:
                            # No indentation, not first block: remove bullet and merge with previous line
                            cursor = self.textCursor()
                            cursor.setPosition(start)
                            cursor.setPosition(start + 2, QTextCursor.KeepAnchor)
                            cursor.removeSelectedText()
                            cursor.deletePreviousChar()  # delete preceding newline to merge blocks
                            self.setTextCursor(cursor)
                        else:
                            # Indented bullet, or first block: remove bullet only, cursor stays at content start
                            new_line = " " * spaces + line[spaces + 2:]
                            self._set_line_text(start, new_line, start + spaces)
                    else:
                        new_spaces = max(0, spaces - 2)
                        new_line = " " * new_spaces + line[spaces:]
                        self._set_line_text(start, new_line, start + new_spaces)
                self.ensureCursorVisible()
                return
            else:
                # A run of Backspace is one step of Undo (_typing).
                with self._typing(BACKWARD) as cursor:
                    if cursor.hasSelection():
                        cursor.removeSelectedText()
                    else:
                        cursor.deletePreviousChar()
                self.ensureCursorVisible()
                return

        elif e.key() == Qt.Key_Delete:
            mention = rich_text.mention_at(c) if self._holds_structure() else None
            if (mention is not None and mention[0] <= c.position() < mention[1]
                    and e.modifiers() in (Qt.NoModifier, Qt.KeypadModifier)):
                self._remove_mention(*mention, FORWARD)
                return
            # A run of Delete is one step of Undo (_typing).
            with self._typing(FORWARD) as cursor:
                if cursor.hasSelection():
                    cursor.removeSelectedText()
                else:
                    cursor.deleteChar()
            self.ensureCursorVisible()
            return

        # A typed character: in the run of typing going on, one step of
        # Undo with it (_typing).
        if e.text() and not e.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            fmt = self.currentCharFormat()
            with self._typing(TYPED) as cursor:
                if cursor.hasSelection():
                    cursor.removeSelectedText()
                cursor.insertText(e.text(), fmt)
            self._after_typed(e.text())
            self.ensureCursorVisible()
            return

        if e.text() and e.text().isprintable():
            # Typed with Option or AltGr ("@", "€", "[" on many keyboards):
            # in the run of typing going on.
            with self._typing(TYPED):
                super().keyPressEvent(e)
            self._after_typed(e.text())
        else:
            # Qt's own keys: what one of them changes (Control-K on a Mac
            # deletes to the end of the line) is a step of typing. A key
            # that deletes is handled in an edit block, so that a photo
            # group it changed is put right in the same step; a key that
            # moves the caret is not (Qt finds no line inside one).
            before = self.document().availableUndoSteps()
            if any(e.matches(key) for key in _QT_DELETING_KEYS):
                whole = QTextCursor(self.document())
                with self._groups_kept():
                    whole.beginEditBlock()
                    try:
                        super().keyPressEvent(e)
                    finally:
                        whole.endEditBlock()
            else:
                super().keyPressEvent(e)
            if self.document().availableUndoSteps() != before:
                self._name_step(before, N_("Typing"))
        self.ensureCursorVisible()

    # -------- Deleting a word or to the end of the line --------
    @staticmethod
    def _deletion_reach(e) -> Optional[str]:
        """"word" or "line" when ``e`` deletes that far, as the platform's
        text fields do: Option-Delete and Fn-Option-Delete a word on a Mac,
        Command-Delete and Fn-Command-Delete to the line's start or end;
        Ctrl+Backspace and Ctrl+Delete a word on Windows and Linux. Else
        None."""
        if e.key() not in (Qt.Key_Backspace, Qt.Key_Delete):
            return None
        modifiers = e.modifiers() & ~Qt.KeypadModifier
        mac = sys.platform == "darwin"
        if modifiers == (Qt.AltModifier if mac else Qt.ControlModifier):
            return "word"
        if mac and modifiers == Qt.ControlModifier:          # Command
            return "line"
        return None

    def _delete_reach(self, kind: str, reach: str) -> None:
        """Delete a word or to the line's edge before the caret (``kind``
        BACKWARD) or after it (FORWARD), in the run of deleting. A mention
        it reaches into goes whole."""
        with self._typing(kind) as cursor:
            keep = QTextCursor.MoveMode.KeepAnchor
            if reach == "line":
                cursor.movePosition(QTextCursor.MoveOperation.StartOfLine if kind == BACKWARD
                                    else QTextCursor.MoveOperation.EndOfLine, keep)
            elif sys.platform == "darwin" and self._mac_word(cursor, kind):
                pass                    # as the Mac's own text view finds words
            elif kind == BACKWARD:
                cursor.movePosition(QTextCursor.MoveOperation.PreviousWord, keep)
            else:
                # To the start of the next word, as on Windows and Linux.
                cursor.movePosition(QTextCursor.MoveOperation.NextWord, keep)
            low, high = cursor.selectionStart(), cursor.selectionEnd()
            for at in (low, high):
                probe = QTextCursor(self.document())
                probe.setPosition(at)
                mention = rich_text.mention_at(probe) if self._holds_structure() else None
                if mention is not None and mention[0] < at < mention[1]:
                    low, high = min(low, mention[0]), max(high, mention[1])
            cursor.setPosition(low)
            cursor.setPosition(high, keep)
            cursor.removeSelectedText()

    @staticmethod
    def _mac_word(cursor: QTextCursor, kind: str) -> bool:
        """Select the word Option-Delete (``kind`` BACKWARD) or
        Fn-Option-Delete deletes from ``cursor``, where AppKit's text view
        finds it (mac_words.py). False when AppKit cannot be asked."""
        block = cursor.block()
        found = mac_words.deleted_range(block_units(block), cursor.positionInBlock(),
                                        forward=kind == FORWARD)
        if found is None:
            return False
        cursor.setPosition(block.position() + found[0])
        cursor.setPosition(block.position() + found[1], QTextCursor.MoveMode.KeepAnchor)
        return True

    # -------- Keys inside Markdown structure --------
    def _structure_key(self, e) -> bool:
        """Enter, Backspace and Tab where the paragraph has a structure of
        its own (a heading, a list item). True when the key was handled."""
        cursor = self.textCursor()
        block = cursor.block()
        plain = e.modifiers() in (Qt.NoModifier, Qt.KeypadModifier)
        key = e.key()
        if rich_text.is_code_block(block) and self._holds_structure():
            return self._code_key(e, cursor, block)
        if cursor.currentTable() is not None and self._holds_structure() \
                and self._table_key(e, cursor):
            return True
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and e.modifiers() in (
                Qt.NoModifier, Qt.ShiftModifier):
            delta = -1 if key == Qt.Key_Backtab or e.modifiers() == Qt.ShiftModifier else 1
            with self.undo_step(_INDENT_STEPS[delta]):
                indented = rich_text.change_indent(cursor, delta)
            if indented:
                self.setTextCursor(cursor)
                return True
            # Tab at the start of a plain paragraph starts a list (the
            # habit typed bullets taught), where lists can be kept.
            if (delta > 0 and not cursor.hasSelection() and cursor.atBlockStart()
                    and self._holds_structure()
                    and not self._indent_level_and_has_bullet(block.text())[0]
                    and not self._indent_level_and_has_bullet(block.text())[1]):
                self.toggle_list(rich_text.BULLET)
                return True
            return False
        if cursor.hasSelection():
            return False
        if rich_text.is_divider(block):
            return self._divider_key(e, cursor, block)
        previous = block.previous()
        if (key == Qt.Key_Backspace and plain and cursor.atBlockStart()
                and previous.isValid() and rich_text.is_divider(previous)):
            # Backspace just below a divider takes the divider away.
            with self._typing(BACKWARD, cursor):
                rich_text.remove_divider(previous)
            return True
        if (rich_text.quote_depth(block) and block.textList() is None and plain and (
                (key in (Qt.Key_Return, Qt.Key_Enter) and not block.text())
                or (key == Qt.Key_Backspace and cursor.atBlockStart()))):
            # Return on an empty quoted line (in the run of typing), or
            # Backspace at the start of a quoted paragraph: one level of
            # quote less.
            with self._typing(TYPED if key != Qt.Key_Backspace else BACKWARD, cursor):
                rich_text.set_quote_depth(block, rich_text.quote_depth(block) - 1)
            self.viewport().update()
            return True
        if block.textList() is not None and plain and (
                (key in (Qt.Key_Return, Qt.Key_Enter) and not block.text())
                or (key == Qt.Key_Backspace and cursor.atBlockStart())):
            # Return on an empty item (in the run of typing), or Backspace
            # at an item's start: one level up, and out of the list from the
            # top level.
            with self._typing(TYPED if key != Qt.Key_Backspace else BACKWARD, cursor):
                rich_text.change_indent(cursor, -1)
            self.setTextCursor(cursor)
            return True
        if rich_text.is_task(block) and plain and key in (Qt.Key_Return, Qt.Key_Enter):
            # The new item of a checklist starts unticked, as in Notes, and
            # the item the caret was in keeps its tick: at its start, the
            # new item is the empty one above it.
            at_start = cursor.atBlockStart() and bool(block.text())
            with self._typing(TYPED, cursor):
                tick = block.blockFormat().marker()
                cursor.insertBlock()
                if at_start:
                    rich_text.set_tick(cursor.block().previous(), rich_text.UNTICKED)
                    rich_text.set_tick(cursor.block(), tick)
                else:
                    rich_text.set_tick(cursor.block(), rich_text.UNTICKED)
            self.setTextCursor(cursor)
            return True
        heading = block.blockFormat().headingLevel()
        if heading and e.key() in (Qt.Key_Return, Qt.Key_Enter) and plain:
            # Return at the end of a heading starts a Body paragraph, the
            # way Pages and every Markdown editor continue after a title;
            # anywhere else in it, both halves stay headings.
            with self._typing(TYPED, cursor):
                fmt = block.blockFormat()
                if cursor.atBlockEnd():
                    fmt = rich_text.heading_block_format(fmt, 0)
                    char = rich_text.body_char_format(cursor.charFormat(), own_weight=False)
                else:
                    char = cursor.charFormat()
                cursor.insertBlock(fmt, char)
            self.setTextCursor(cursor)
            self.setCurrentCharFormat(char)
            self._update_active_format()
            return True
        if heading and e.key() == Qt.Key_Backspace and plain and cursor.atBlockStart():
            # Backspace at the start of a heading makes it Body first.
            self.set_heading(0)
            return True
        return False

    def _table_key(self, e, cursor) -> bool:
        """The keys of a table. Tab goes to the next cell (after the last
        one it adds a row), Shift+Tab to the one before; Return goes to the
        cell below (a new row after the last), since a Markdown cell holds
        one line; Command-Return on a Mac and Ctrl+Return elsewhere leave
        the table, into a paragraph below it. True when the key was
        handled."""
        key = e.key()
        modifiers = e.modifiers() & ~Qt.KeypadModifier
        moved = False
        # A row or a paragraph these keys make is in the run of typing.
        with self._typing(TYPED, cursor):
            if key == Qt.Key_Tab and modifiers == Qt.NoModifier:
                moved = rich_text.next_table_cell(cursor, 1)
            elif key == Qt.Key_Backtab or (key == Qt.Key_Tab and modifiers == Qt.ShiftModifier):
                moved = rich_text.next_table_cell(cursor, -1)
            elif key in (Qt.Key_Return, Qt.Key_Enter) and modifiers == Qt.ControlModifier:
                moved = rich_text.leave_table(cursor)
            elif key in (Qt.Key_Return, Qt.Key_Enter) and modifiers in (Qt.NoModifier,
                                                                        Qt.ShiftModifier):
                # A Markdown cell holds one line: Return goes to the cell
                # below, as in a spreadsheet (a new row after the last one).
                moved = rich_text.next_table_row(cursor)
        if moved:
            self.setTextCursor(cursor)
        return moved

    @_named_step(N_("Table"))
    def insert_table(self, rows: int, columns: int) -> None:
        """Insert > Table: a table of ``rows`` (the header included) and
        ``columns``, the caret in its first cell."""
        cursor = self.textCursor()
        if rich_text.insert_table(cursor, rows, columns) is not None:
            self.setTextCursor(cursor)
            self.setCurrentCharFormat(cursor.block().charFormat())

    TABLE_COMMANDS = {
        "add_row_above": lambda c: rich_text.add_table_row(c, below=False),
        "add_row_below": lambda c: rich_text.add_table_row(c, below=True),
        "add_column_before": lambda c: rich_text.add_table_column(c, after=False),
        "add_column_after": lambda c: rich_text.add_table_column(c, after=True),
        "delete_row": rich_text.delete_table_row,
        "delete_column": rich_text.delete_table_column,
        "delete_table": rich_text.delete_table,
    }

    # What Edit > Undo calls each: the command's own name.
    TABLE_STEPS = {
        "add_row_above": N_("Add Row Above"), "add_row_below": N_("Add Row Below"),
        "add_column_before": N_("Add Column Before"),
        "add_column_after": N_("Add Column After"),
        "delete_row": N_("Delete Row"), "delete_column": N_("Delete Column"),
        "delete_table": N_("Delete Table"),
    }

    def table_command(self, name: str) -> None:
        """Format > Table: a row or a column added or deleted, or the
        table deleted, at the caret."""
        cursor = self.textCursor()
        with self.undo_step(self.TABLE_STEPS[name]):
            done = self.TABLE_COMMANDS[name](cursor)
        if done:
            self.setTextCursor(cursor)
            self.setCurrentCharFormat(cursor.charFormat())

    CODE_INDENT = 4

    def _code_key(self, e, cursor, block) -> bool:
        """The keys of a code block. Tab indents by four spaces and
        Shift+Tab outdents; Return (and Shift+Return) always starts the
        next line, as far in as the caret's line, so blank lines between
        functions stay code (ProseMirror's base keymap, as in Notion and
        Google Docs). The ways out: Command-Return (Ctrl+Return) from any
        line, as in a table, Down on the document's last line, Backspace
        at the start of the block, or Body from the Style pop-up. True when
        the key was handled."""
        key = e.key()
        plain = e.modifiers() in (Qt.NoModifier, Qt.KeypadModifier)
        shift = e.modifiers() == Qt.ShiftModifier
        if key in (Qt.Key_Return, Qt.Key_Enter) and (
                e.modifiers() & ~Qt.KeypadModifier) == Qt.ControlModifier:
            self._leave_code_block(cursor, block)
            return True
        if key == Qt.Key_Backtab or (key == Qt.Key_Tab and shift):
            self._outdent_code(cursor)
            return True
        if key == Qt.Key_Tab and plain:
            if cursor.hasSelection() and "\u2029" in cursor.selectedText():
                self._indent_code_lines(cursor, +1)
            else:
                column = cursor.positionInBlock()
                with self._typing(TYPED, cursor):
                    cursor.insertText(" " * (self.CODE_INDENT - column % self.CODE_INDENT))
            return True
        if cursor.hasSelection():
            return False
        _first, last = rich_text.code_run(block)
        if key in (Qt.Key_Return, Qt.Key_Enter) and (plain or shift):
            # The new line starts as far in as the caret's line (after any
            # line break in the block), no further than the caret.
            text = block.text()
            before = text[:text_index(text, cursor.positionInBlock())]
            line = before[before.rfind(LINE_BREAK) + 1:]
            indent = line[:len(line) - len(line.lstrip(" \t"))]
            with self._typing(TYPED, cursor):
                cursor.insertBlock()
                if indent:
                    cursor.insertText(indent)
            self.setTextCursor(cursor)
            return True
        if key == Qt.Key_Down and plain and not block.next().isValid():
            cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
            self.setTextCursor(cursor)
            # The paragraph below, in the run of typing with what follows.
            with self._typing(TYPED, cursor):
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            self.setCurrentCharFormat(QTextCharFormat())
            return True
        column = cursor.positionInBlock()
        lead = len(block.text()) - len(block.text().lstrip(" "))
        if key == Qt.Key_Backspace and plain and 0 < column <= lead:
            # In the indent, back to the last stop of four, as Tab went.
            with self._typing(BACKWARD, cursor):
                cursor.setPosition(block.position() + (column - 1) // self.CODE_INDENT
                                   * self.CODE_INDENT, QTextCursor.MoveMode.KeepAnchor)
                cursor.removeSelectedText()
            return True
        if key == Qt.Key_Backspace and plain and cursor.atBlockStart() and block == _first:
            with self._typing(BACKWARD, cursor):
                rich_text.leave_code(block)
            self.setCurrentCharFormat(QTextCharFormat())
            return True
        return False

    def _leave_code_block(self, cursor, block) -> None:
        """Out of the code block, into a new Body paragraph right after it
        (in the run of typing, as Return's paragraph is)."""
        _first, last = rich_text.code_run(block)
        cursor.setPosition(last.position() + last.length() - 1)
        self.setTextCursor(cursor)
        with self._typing(TYPED, cursor):
            cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        self.setCurrentCharFormat(QTextCharFormat())

    def _indent_code_lines(self, cursor, delta: int) -> None:
        """Four spaces more (+1) or less (-1) at the start of every line of
        code under the cursor, as one step on the undo stack, named as
        Increase and Decrease Indent are."""
        doc = self.document()
        lines = []
        block = doc.findBlock(cursor.selectionStart())
        last = doc.findBlock(cursor.selectionEnd())
        while block.isValid():
            if rich_text.is_code_block(block):
                lines.append(block)
            if block == last:
                break
            block = block.next()
        edit = QTextCursor(doc)
        before = doc.availableUndoSteps()
        edit.beginEditBlock()
        for line in lines:
            start = QTextCursor(line)
            if delta > 0:
                start.insertText(" " * self.CODE_INDENT)
            else:
                text = line.text()
                spaces = len(text) - len(text.lstrip(" "))
                start.setPosition(line.position() + min(spaces, self.CODE_INDENT),
                                  QTextCursor.MoveMode.KeepAnchor)
                start.removeSelectedText()
        edit.endEditBlock()
        self._name_step(before, _INDENT_STEPS[delta])

    def _outdent_code(self, cursor) -> None:
        self._indent_code_lines(cursor, -1)

    @_named_step(N_("Code Block"))
    def make_code_block(self, language: str = "") -> None:
        """Code Block: the paragraphs under the cursor become one."""
        cursor = self.textCursor()
        rich_text.make_code_block(cursor, language)
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(cursor.block().charFormat())

    @_named_step(N_("Code Language"))
    def set_code_language(self, language: str) -> None:
        cursor = self.textCursor()
        rich_text.set_code_language(cursor, language)
        self.setTextCursor(cursor)

    def _divider_key(self, e, cursor, block) -> bool:
        """A divider holds no text: typing on it goes into the paragraph
        below (made when there is none), Backspace and Delete take it
        away."""
        key = e.key()
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            with self._typing(BACKWARD if key == Qt.Key_Backspace else FORWARD, cursor):
                rich_text.remove_divider(block)
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter) or (
                e.text() and e.text().isprintable()
                and not e.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)):
            below = block.next()
            if not below.isValid() or rich_text.is_divider(below) or \
                    key in (Qt.Key_Return, Qt.Key_Enter):
                cursor.movePosition(QTextCursor.EndOfBlock)
                self.setTextCursor(cursor)
                # In the run of typing, with what is typed after it.
                with self._typing(TYPED, cursor):
                    cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            else:
                cursor.setPosition(below.position())
            self.setTextCursor(cursor)
            # Return is done; a character is typed where the caret is now.
            return key in (Qt.Key_Return, Qt.Key_Enter)
        return False

    def _apply_active_format_to_cursor(self):
        """Apply the active formatting state to the current cursor position."""
        fmt = QTextCharFormat()
        if self.active_format['bold']:
            fmt.setFontWeight(700)
        if self.active_format['italic']:
            fmt.setFontItalic(True)
        if self.active_format['underline']:
            fmt.setFontUnderline(True)
        if self.active_format['color']:
            fmt.setForeground(self.active_format['color'])
        self.mergeCurrentCharFormat(fmt)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            if self._drop_check(event.mimeData().urls()):
                event.acceptProposedAction()
            else:
                event.ignore()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        # Picture files over a photo group: the slot they would take.
        if not self.photos.pointer.drag_files(event):
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self.photos.pointer.stop()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        # Picture files let go of over the slot shown: they go there.
        if self.photos.pointer.drop_files(event):
            return
        urls = list(event.mimeData().urls()) if event.mimeData().hasUrls() else []
        # Files go to whoever handles them; addresses from the web dropped
        # in plain text are text, as in TextEdit's plain text.
        if (urls and (self._holds_structure() or any(url.isLocalFile() for url in urls))
                and self.isSignalConnected(QMetaMethod.fromSignal(self.urls_dropped))):
            # What is dropped goes where it is dropped, as in Pages and
            # TextEdit: the caret moves there, and the listener inserts at it.
            self.setTextCursor(self.cursorForPosition(event.position().toPoint()))
            self.urls_dropped.emit(list(event.mimeData().urls()))
            event.acceptProposedAction()
        else:
            # Text goes where it was dropped, the way a paste would put it
            # there (insertFromMimeData), as one step on the undo stack.
            # Qt makes the drop one step around the paste it does, so the
            # step is named here, as a whole.
            before = self.document().availableUndoSteps()
            self._mime_step = ""
            try:
                # What the drag took away is part of the step too (a
                # picture dragged out of a photo group).
                with self._groups_kept():
                    super().dropEvent(event)
            finally:
                self._mime_step = N_("Paste")
            self._name_step(before, N_("Drag"))

    @_named_step(N_("Cut"))
    def cut(self):
        super().cut()

    # -------- Paste --------
    def paste_from_clipboard(self):
        """Paste (Edit > Paste, the context menu): the same as the
        keyboard's paste, which Qt sends to insertFromMimeData."""
        self.insertFromMimeData(QApplication.clipboard().mimeData())

    @_named_step(N_("Paste"))
    def paste_normalized(self):
        """Paste and Match Style: the clipboard's text, in the style of the
        text where it goes (in plain text, files as their paths, as Paste
        puts them)."""
        source = QApplication.clipboard().mimeData()
        self._insert_plain(self._plain_file_paths(source) or paste.plain_text(source))

    def _plain_file_paths(self, source) -> str:
        """In a plain-text or code document, the files ``source`` holds as
        their paths, one a line, as TextEdit puts a file in plain text;
        else (or for anything else) ""."""
        if self._holds_structure() or source is None or not source.hasUrls():
            return ""
        urls = list(source.urls())
        if not urls or not all(url.isLocalFile() for url in urls):
            return ""
        return "\n".join(file_paths.normalize(url.toLocalFile()) for url in urls)

    def canInsertFromMimeData(self, source):
        """What a paste or a drop can bring here: what insertFromMimeData
        takes, a picture on its own included while someone handles media
        (``_pasted_media``). Edit > Paste, a drop and the keys then agree
        (U1 of the CP3 fixes review: a screenshot dimmed Paste while
        Command-V pasted it)."""
        return (source.hasFormat(paste.LITERAL_MIME) or source.hasFormat(paste.MARKDOWN_MIME)
                or super().canInsertFromMimeData(source)
                or (source.hasImage() and self._keeps_pictures() and self.isSignalConnected(
                    QMetaMethod.fromSignal(self.image_pasted))))

    def createMimeDataFromSelection(self):
        """Copy: the text and HTML other apps read, and, where the document
        holds Markdown structure, the selection as this editor writes
        Markdown, so a paste here keeps every structure exactly. (Qt's own
        copy would offer Qt's Markdown, which reads a heading's weight as
        bold, and builds an OpenDocument copy of every selection.)

        The data is Qt's own, made by QTextEdit, with these formats set on
        it: the clipboard keeps it after MyEditor quits, and Qt frees it
        after the Python interpreter has gone, which data made in Python
        does not survive (a crash at every quit after a Copy). It is made
        where nothing is selected (_EmptyCopy), so Qt writes no HTML and no
        OpenDocument copy of the selection that these would replace."""
        cursor = self.textCursor()
        # A photo group goes as its paragraphs: a picture and its caption
        # each, which every app and a paste here read (photo_group).
        fragment = photo_group.unfolded_fragment(cursor.selection())
        if self._empty_copy is None:
            self._empty_copy = _EmptyCopy(self)
        mime = self._empty_copy.qt_data()
        # What other apps read: the app's own objects as their address (or
        # name), never their internal name.
        mime.setText(rich_text.plain_text_of(self._outside_copy(html=False)))
        mime.setHtml(self._outside_copy(html=True).toHtml())
        # What a paste here reads (private): the objects themselves.
        mime.setData(paste.LITERAL_HTML_MIME, fragment.toHtml().encode("utf-8"))
        if not self._holds_structure():
            return mime
        mime.setData(paste.MARKDOWN_MIME, paste.selection_markdown(
            cursor, outside=self._outside_of).encode("utf-8"))
        # The words as they are, the structure as Markdown.
        mime.setData(paste.LITERAL_MIME,
                     paste.selection_markdown(cursor, literal=True).encode("utf-8"))
        return mime

    def _outside_copy(self, *, html: bool):
        """The selection as other apps get it: each of the app's own objects
        as its Outside. In ``html``, audio and video as a link to it and a
        picture as its address or its bytes; in text, each as its address
        or its name, never the object's stand-in character. A photo group,
        as its paragraphs."""
        fragment = photo_group.unfolded_fragment(self.textCursor().selection())
        if self._outside is None:
            return fragment
        doc = QTextDocument()
        QTextCursor(doc).insertFragment(fragment)
        found = []
        for block in iter_blocks(doc):
            it = block.begin()
            while not it.atEnd():
                piece = it.fragment()
                fmt = piece.charFormat()
                outside = (self._outside_of(fmt.toImageFormat().name())
                           if piece.isValid() and fmt.isImageFormat() else None)
                if outside is not None:
                    found.append((piece.position(), piece.length(), outside,
                                  fmt.toImageFormat()))
                it += 1
        for position, length, outside, image in reversed(found):
            cursor = QTextCursor(doc)
            cursor.setPosition(position)
            cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
            if html and not outside.media and outside.src:
                image.setName(outside.src)
                cursor.insertImage(image)
                continue
            words = QTextCharFormat()
            if html and outside.href:
                words.setAnchor(True)
                words.setAnchorHref(outside.href)
            cursor.insertText(outside.text, words)
        return QTextDocumentFragment(doc)

    def insertFromMimeData(self, source):
        """Every paste and every drop of text arrives here (_insert_mime);
        audio or video in what came in then stands in a paragraph of its
        own, wherever it landed (_set_media_apart)."""
        start = self.textCursor().selectionStart()
        with self.undo_step(self._mime_step):
            self._insert_mime(source)
            self._set_media_apart(start, self.textCursor().position())

    def _set_media_apart(self, start: int, end: int) -> None:
        """Audio and video between ``start`` and ``end`` sharing a paragraph
        with words get one of their own: the paragraph is split before and
        after it, the words on each side keeping their paragraph's format.
        Part of the same step of Undo as what brought it there."""
        if self._outside is None:
            return
        doc = self.document()
        found = []
        block = doc.findBlock(start)
        while block.isValid() and block.position() <= end:
            if block.text() != "\ufffc":
                it = block.begin()
                while not it.atEnd():
                    fragment = it.fragment()
                    fmt = fragment.charFormat()
                    if fmt.isImageFormat() and self._is_media(fmt.toImageFormat().name()):
                        found.extend(range(fragment.position(),
                                           fragment.position() + fragment.length()))
                    it += 1
            block = block.next()
        if not found:
            return
        cursor = QTextCursor(doc)
        cursor.joinPreviousEditBlock()
        for position in reversed(found):
            cursor.setPosition(position)
            block = cursor.block()
            words_format = block.blockFormat()
            if position + 1 < block.position() + block.length() - 1:
                after = QTextCursor(doc)
                after.setPosition(position + 2)
                cursor.setPosition(position + 1)
                cursor.insertBlock(words_format, after.charFormat())
            if position > block.position():
                cursor.setPosition(position)
                cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
            else:
                cursor.setBlockFormat(QTextBlockFormat())     # a plain paragraph
        cursor.endEditBlock()

    def _insert_mime(self, source):
        """Every paste and every drop of text arrives here.

        - A web address pasted over words makes them a link to it (over
          an address, it replaces it: the words would say one address
          and the link go to another).
        - Pictures (image files copied in a file manager, a picture copied
          on its own) go to whoever handles media, when someone does.
        - In a document that holds Markdown structure, outside a code
          block, the paste comes with what Markdown can say of it
          (paste.py); elsewhere, and for text that is not Markdown, it is
          plain text in the style of the text where it goes."""
        if source is None:
            return
        self._step_out_of_mention()
        if self._paste_guard is not None:
            words = paste.plain_text(source)
            allowed = self._paste_guard(words) if words else words
            if allowed is None:
                return
            if allowed != words:
                self._insert_plain(allowed)
                return
        cursor = self.textCursor()
        text = source.text() if source.hasText() else ""
        address = text.strip()
        if (cursor.hasSelection() and link_url.is_bare_http_url(address)
                and self._holds_structure() and not rich_text.is_code_block(cursor.block())
                and "\u2029" not in cursor.selectedText()
                and not link_url.is_an_address(cursor.selectedText())):
            rich_text.set_link(cursor, "", address)
            cursor.setPosition(cursor.selectionEnd())
            self.setTextCursor(cursor)
            self._leave_link_at_end()
            return
        if self._pasted_media(source, text):
            return
        paths = self._plain_file_paths(source)
        if paths:
            self._insert_plain(paths)
            return
        if (self._keeps_local_formats() and source.hasFormat(paste.LITERAL_HTML_MIME)
                and not rich_text.is_code_block(cursor.block())):
            # Copied here, pasted into a file that keeps underline and
            # colors: Qt's own copy of it, with them (and its objects).
            own = QTextDocument()
            own.setHtml(bytes(source.data(paste.LITERAL_HTML_MIME)).decode("utf-8"))
            rich_text.insert_document(cursor, own)
            self.setTextCursor(cursor)
            self.ensureCursorVisible()
            return
        if self._holds_structure() and not rich_text.is_code_block(cursor.block()):
            pasted = paste.from_mime(source)
            if pasted is not None:
                if pasted.left_out:
                    self.notice.emit(_n_pictures_left_out(pasted.left_out))
                if not pasted.document.isEmpty():
                    rich_text.insert_document(cursor, pasted.document)
                    self.setTextCursor(cursor)
                    self.ensureCursorVisible()
                    return
        if getattr(self, "_markdown_source", False) and source.hasFormat(paste.MARKDOWN_MIME):
            # Markdown opened as its text: what was copied, as Markdown.
            text = bytes(source.data(paste.MARKDOWN_MIME)).decode("utf-8", "replace")
        self._insert_plain(text or paste.plain_text(source))

    def _pasted_media(self, source, text: str) -> bool:
        """Report pasted pictures to whoever handles media. True when
        someone took them."""
        if source.hasUrls() and self.isSignalConnected(
                QMetaMethod.fromSignal(self.urls_dropped)):
            urls = list(source.urls())
            if urls and all(u.isLocalFile() and (
                    image_safety.is_image_file(u.toLocalFile())
                    or (self.nostr_in_use() and media_files.is_audio_or_video(u.toLocalFile())))
                    for u in urls):
                # Image files copied in the Finder or Explorer, and, while
                # a Nostr account is in use, audio and video, as a drop.
                self.urls_dropped.emit(urls)
                return True
        address = text.strip()
        if (link_url.is_bare_http_url(address) and self._keeps_pictures()
                and self._in_library(address) and self.isSignalConnected(
                    QMetaMethod.fromSignal(self.urls_dropped))):
            # A file's link copied in the Media Library: the file, shown
            # where it goes, as a tile dragged from there puts it.
            self.urls_dropped.emit([QUrl(address)])
            return True
        if source.hasImage() and not text.strip() and self._keeps_pictures() and \
                self.isSignalConnected(QMetaMethod.fromSignal(self.image_pasted)):
            image = source.imageData()
            if isinstance(image, QPixmap):
                image = image.toImage()
            if isinstance(image, QImage) and not image.isNull():
                self.image_pasted.emit(image)
                return True
        return False

    def _insert_plain(self, text: str) -> None:
        """Insert plain text in the style of the text where it goes (the
        way typing would: not into a link it only touches). A table cell
        holds one line, so line breaks become spaces there."""
        if not text:
            return
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            cursor.removeSelectedText()
            self.setTextCursor(cursor)
        if cursor.currentTable() is not None and self._holds_structure():
            text = re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", text.strip())
        self._leave_link_at_end()
        cursor.insertText(text, self.currentCharFormat())
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
