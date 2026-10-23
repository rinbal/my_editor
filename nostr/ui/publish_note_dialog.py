# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publish as Note: a sheet on the document's window that publishes a
short note (kind 1).

The note is composed here, as a share sheet composes what it shares:
it starts as the document's text in plain words (what apps show for a
note) and can be changed before it goes out; the document is not
changed by it.

A mention shows as "@" and the person's name, never as their key, as in
the editor (rich_text: a link to the person's ``nostr:`` address): the
note's text, pasted text holding ``nostr:npub1…``, and a mention chosen
by typing "@" at the start of a word. Backspace takes a mention away
whole, and what is typed inside one goes after it. Published (and
copied), each mention is its ``nostr:`` address again, which every Nostr
app shows as the name, and the person gets a ``p`` tag (NIP-27).

People shows everyone the note will tag, from the event the publisher
builds; "Publishing as" chooses the account, as in Publish as Article.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QTextCursor, QTextDocumentFragment
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout,
    QWidget,
)

import rich_text
from doc_walk import text_index
from alerts import answer_return_as_the_system_does
from highlighter import RichTextLook
from i18n import _, ngettext

from .. import refs
from ..avatar_store import AvatarStore
from ..bech32 import encode_nprofile, encode_npub
from ..bunker import BunkerSessionPool, humanize_failure
from ..known_people import KnownPeople
from ..nip05_check import Nip05Check
from ..outbox import RelayDirectory, relays_from
from ..profiles import Profile, ProfileStore
from ..publisher import PublishedMedia, PublishJob, PublishResult, build_note
from ..relay import RelayPool
from ..search import Nip50SearchClient
from .mention_picker import MentionPicker
from . import appearance
from .publish_sheet import (
    AccountRow, BottomRow, PeopleRow, StatusLine, fit_margins, fit_to_window, make_sheet,
    people_tagged, scroll_area, sheet_stylesheet,
)
from .secret_key_check import publishable


# Length above which we softly suggest using long-form (kind 30023) instead.
_LONGFORM_HINT_CHARS = 1_000


class NoteText(QTextEdit):
    """The note's text: plain words, with mentions as names (see the
    module docstring). ``mention_requested(at)`` for an "@" typed at the
    start of a word, at its position."""

    mention_requested = Signal(int)

    def __init__(self, label_for: Callable[[str], Optional[str]], *, is_dark: bool,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # How a person a ``nostr:`` address names is called, or None when
        # it names no person.
        self._label_for = label_for
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        # Mentions as names in the link colour, as in the editor.
        self._look = RichTextLook(self.document(), is_dark)

    def set_dark(self, is_dark: bool) -> None:
        self._look.set_theme(is_dark)

    def set_note(self, text: str) -> None:
        self.setPlainText(text)
        rich_text.link_references(self.document(), self._label_for)
        self.document().clearUndoRedoStacks()

    def note(self) -> str:
        """The note as it is published: each mention its ``nostr:`` address,
        its no-break spaces as written."""
        return rich_text.plain_text_of(QTextDocumentFragment(self.document()),
                                       as_written=True).strip()

    def insert_mention(self, at: int, href: str, name: str) -> None:
        """The mention of ``name`` in place of the "@" typed at ``at`` (when
        it is still there), else at the caret."""
        cursor = self.textCursor()
        doc = self.document()
        if 0 <= at < doc.characterCount() and doc.characterAt(at) == "@":
            cursor.setPosition(at)
            cursor.setPosition(at + 1, QTextCursor.MoveMode.KeepAnchor)
        rich_text.insert_mention(cursor, href, name)
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(rich_text.without_link(self.currentCharFormat()))

    # -- typing ---------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802, Qt's name
        cursor = self.textCursor()
        mention = rich_text.mention_at(cursor)
        plain = event.modifiers() in (Qt.KeyboardModifier.NoModifier,
                                      Qt.KeyboardModifier.KeypadModifier)
        if mention is not None and plain and (
                event.key() == Qt.Key.Key_Backspace and mention[0] < cursor.position()
                or event.key() == Qt.Key.Key_Delete and cursor.position() < mention[1]):
            # A mention goes as a whole: half a name links to nobody.
            cursor.setPosition(mention[0])
            cursor.setPosition(mention[1], QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()
            self.setTextCursor(cursor)
            return
        typed = event.text()
        if typed and typed.isprintable():
            self._type_outside_mentions()
        super().keyPressEvent(event)
        if typed == "@":
            self._maybe_ask_mention()

    def inputMethodEvent(self, event) -> None:  # noqa: N802, Qt's name
        if event.commitString() or event.preeditString():
            self._type_outside_mentions()
        super().inputMethodEvent(event)
        if event.commitString() == "@":
            self._maybe_ask_mention()

    def _type_outside_mentions(self) -> None:
        """What is typed is never part of a mention: inside one it goes
        after it, beside one it is plain."""
        cursor = self.textCursor()
        mention = rich_text.mention_at(cursor)
        if mention is not None and mention[0] < cursor.position() < mention[1]:
            cursor.setPosition(mention[1])
            self.setTextCursor(cursor)
        self.setCurrentCharFormat(rich_text.without_link(self.currentCharFormat()))

    def _maybe_ask_mention(self) -> None:
        """An "@" was just typed: ask whom to mention when it starts a word
        (not in an email address)."""
        cursor = self.textCursor()
        text = cursor.block().text()
        before = text[:text_index(text, cursor.positionInBlock()) - 1]
        if before and not (before[-1].isspace() or before[-1] in "([{\"'“‘"):
            return
        self.mention_requested.emit(cursor.position() - 1)

    # -- the clipboard ----------------------------------------------------------------

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802, Qt's name
        """Pasted as plain words, a ``nostr:`` address of a person shown as
        their name."""
        if not source.hasText():
            return
        self._type_outside_mentions()
        self.textCursor().insertText(source.text())
        rich_text.link_references(self.document(), self._label_for)

    def createMimeDataFromSelection(self) -> QMimeData:  # noqa: N802, Qt's name
        """Copied, a mention is its ``nostr:`` address, which other apps
        read as the person."""
        data = QMimeData()
        data.setText(rich_text.plain_text_of(self.textCursor().selection()))
        return data


class PublishNoteDialog(QDialog):
    """Compose and publish a note (see the module docstring).

    ``published(event_id, results)`` once a relay took it; the sheet then
    closes. A failure is shown in it, to try again."""

    # Args: (signed_event_id_hex, list[PublishResult])
    published = Signal(str, list)

    def __init__(
        self,
        *,
        content: str,
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
        media: Sequence[PublishedMedia] = (),
        parent=None,
        is_dark: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Publish as Note"))
        make_sheet(self)
        self.setMinimumSize(420, 300)
        # What is known of the pictures, audio and video in the text, for
        # their descriptions (NIP-92 imeta); only those still in the text
        # when it goes out are described.
        self._media = tuple(media)
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
        self._is_dark = is_dark
        self._job: Optional[PublishJob] = None
        self._signed_event_id: Optional[str] = None
        self._picker: Optional[MentionPicker] = None
        self._mention_at = -1

        self._build(active_profile, store)
        self._text_edit.set_note(content)
        self._text_edit.textChanged.connect(self._refresh)
        self._refresh()
        fit_to_window(self, QSize(600, 440))

    # -- the sheet ----------------------------------------------------------------

    def _build(self, profile: Profile, store: ProfileStore) -> None:
        self.setStyleSheet(sheet_stylesheet(self._is_dark))
        appearance.surface(self, self._is_dark)
        appearance.bind(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(8)
        # Everything but the account and the buttons scrolls: in the
        # smallest window there is room for little more.
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        outer.addWidget(scroll_area(page), 1)

        heading = QLabel(_("Publish as Note"))
        heading.setObjectName("title")
        layout.addWidget(heading)
        hint = QLabel(_("Formatting is stripped. Short notes are plain text. "
                        "For richer documents, use Publish as Article."))
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        def label_for(entity: str) -> Optional[str]:
            def name(pubkey: str) -> Optional[str]:
                person = self._known_people.get(pubkey)
                return person.display_name if person is not None else None
            return refs.person_name(entity, name)

        self._text_edit = NoteText(label_for, is_dark=self._is_dark)
        self._text_edit.setAccessibleName(_("Note"))
        self._text_edit.mention_requested.connect(self._ask_mention)
        # A few lines at least, the room there is when there is more.
        self._text_edit.setMinimumHeight(self._text_edit.fontMetrics().lineSpacing() * 3 + 16)
        layout.addWidget(self._text_edit, 1)

        self._char_count = QLabel("")
        self._char_count.setObjectName("help")
        self._char_count.setWordWrap(True)
        layout.addWidget(self._char_count)

        # Everyone the note tags: shown while there is someone.
        self._people_line = QWidget()
        people_row = QHBoxLayout(self._people_line)
        people_row.setContentsMargins(0, 0, 0, 0)
        people_row.setSpacing(10)
        people_label = QLabel(_("People:"))
        people_label.setContentsMargins(0, 3, 0, 0)
        people_row.addWidget(people_label, 0, Qt.AlignmentFlag.AlignTop)
        self._people = PeopleRow(self._known_people, self._avatars)
        self._people.setAccessibleName(_("People"))
        people_row.addWidget(self._people, 1)
        layout.addWidget(self._people_line)

        self._status = StatusLine(self._is_dark)
        outer.addWidget(self._status)

        self._account = AccountRow(profile, store, self._avatars, is_dark=self._is_dark)
        self._account.switched.connect(lambda _profile: self._refresh())
        buttons = QDialogButtonBox()
        self._cancel_btn = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._publish_btn = buttons.addButton(_("Publish"),
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._publish_btn.clicked.connect(self._on_publish)
        outer.addWidget(BottomRow(self._account, buttons))
        QWidget.setTabOrder(self._text_edit, self._account.button)
        answer_return_as_the_system_does(self.findChildren(QPushButton))

    def apply_theme(self, is_dark: bool) -> None:
        """The app's appearance changed while the sheet is open: all of it
        takes the new one, as the window does."""
        self._is_dark = is_dark
        self.setStyleSheet(sheet_stylesheet(is_dark))
        appearance.surface(self, is_dark)
        self._text_edit.set_dark(is_dark)
        self._account.set_dark(is_dark)
        self._status.set_dark(is_dark)
        if self._picker is not None:
            self._picker.set_dark(is_dark)

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        fit_margins(self)

    # -- mentions -----------------------------------------------------------------

    def _ask_mention(self, at: int) -> None:
        """Whom to mention, asked at the caret: the people you follow first,
        then a search; Escape keeps the "@"."""
        if self._picker is None:
            self._picker = MentionPicker(
                self._known_people, self._search_client, avatars=self._avatars, parent=self,
                is_dark=self._is_dark, address_check=self._address_check)
            self._picker.picked.connect(self._mention_picked)
        self._mention_at = at
        self._picker.open_at(self._text_edit.cursorRect(), self._text_edit.viewport())

    def _mention_picked(self, person) -> None:
        self._text_edit.setFocus()
        address = ("nostr:" + encode_nprofile(person.pubkey, [person.relay_hint])
                   if person.relay_hint else "nostr:" + encode_npub(person.pubkey))
        self._text_edit.insert_mention(self._mention_at, address, person.display_name
                                       or refs.short_key(encode_npub(person.pubkey)))

    # -- content / status --------------------------------------------------

    def _content(self) -> str:
        return self._text_edit.note()

    def _refresh(self) -> None:
        content = self._content()
        # As long as it reads: a mention counts as its name.
        n = len(self._text_edit.toPlainText().strip())
        msg = ngettext("{n} character", "{n} characters", n).format(n=n)
        if n >= _LONGFORM_HINT_CHARS:
            msg = _("{count} · long note, consider Publish as Article instead").format(
                count=msg)
        self._char_count.setText(msg)
        self._publish_btn.setEnabled(bool(content) and self._job is None)
        event = build_note(content, self._account.profile().user_pubkey)
        people = people_tagged(event, content, markdown=False)
        self._people.show_people(people)
        self._people_line.setVisible(bool(people))

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status.say(text, error=error)

    def _set_busy(self, busy: bool) -> None:
        self._text_edit.setReadOnly(busy)
        self._account.setEnabled(not busy)
        self._publish_btn.setEnabled(not busy and bool(self._content()))

    # -- publish flow ------------------------------------------------------

    def _on_publish(self) -> None:
        content = self._content()
        if not content:
            return
        if self._job is not None:
            return  # already running

        profile = self._account.profile()
        unsigned = publishable(self, build_note(
            content,
            profile.user_pubkey,
            media=self._media,
        ), is_dark=self._is_dark)
        if unsigned is None:
            return
        self._set_busy(True)
        self._set_status("")
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
            self._set_status(_("The note was not published: no relay took it. Check your "
                               "internet connection and try again."), error=True)
            self._set_busy(False)
            return
        self.published.emit(self._signed_event_id or "", results)
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
        super().reject()
