# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What Publish as Article and Publish as Note share.

Both are sheets on the document's window (window-modal; on macOS they
slide down from its title bar), never taller than that window, in the
theme's colours, light and dark. Both show who the event will tag, and
who publishes it, the same way:

``PeopleRow``
    Everyone the event tags (its ``p`` tags, read from the event the
    publisher builds, so the row is the truth): by name with their
    avatar, never by key, each labelled as mentioned in the text, quoted
    (the author of a note or article the text quotes) or co-author. A
    co-author has × to remove them; the article's row ends in Add
    Co-Author…. A click on a person shows their name and Nostr address.

``AccountRow``
    "Publishing as" with the account's avatar and name, and a menu to
    publish as another account.

``StatusLine``
    Progress in muted words, a failure in the theme's red, above the
    buttons; hidden while there is nothing to say.

``BottomRow``
    "Publishing as" at the leading edge, Cancel and Publish at the
    trailing edge; in a window too narrow for both, the buttons go under
    the account.
"""

from __future__ import annotations

import sys
import shiboken6
from typing import Iterable, List, Optional, Tuple

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, Signal, Slot
from PySide6.QtGui import QFontMetrics, QIcon
from PySide6.QtWidgets import (
    QApplication, QBoxLayout, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPlainTextEdit, QPushButton, QScrollArea, QStyle, QToolButton, QToolTip, QWidget,
)

import announce
from . import appearance
from i18n import _
from popup_placement import fitting_size

from ..article_details import AUTHOR_MARKER
from ..failures import markup, spoken
from ..avatar_store import AvatarStore
from ..bech32 import encode_npub
from ..known_people import KnownPeople
from ..profiles import Profile, ProfileStore
from ..publisher import extract_inline_mentions
from .assistant import stylesheet as assistant_stylesheet
from .avatar import make_avatar_pixmap_from_image, pixmap_for_profile
from .flow_layout import FlowLayout

# How a person is tagged.
MENTIONED = "mentioned"
QUOTED = "quoted"
COAUTHOR = "co-author"

_PERSON_AVATAR = 18
# The account's picture in "Publishing as", and where its arrow ends from
# the button's trailing edge.
_ACCOUNT_AVATAR = 20
_ACCOUNT_ARROW_END = 10
# Room kept between a sheet and the edges of the window it hangs from.
_WINDOW_MARGIN = 24


def make_sheet(dialog: QDialog) -> None:
    """``dialog`` as a sheet on its window: window-modal, and on macOS a
    sheet that slides down from the window's title bar."""
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    if sys.platform == "darwin":
        dialog.setWindowFlag(Qt.WindowType.Sheet, True)


def fit_to_window(dialog: QDialog, wanted: QSize) -> None:
    """Open ``dialog`` at ``wanted``, made small enough for the window it
    is attached to and for the screen: never taller than its window (its
    content scrolls instead)."""
    size = fitting_size(dialog, wanted)
    parent = dialog.parentWidget()
    window = parent.window() if parent is not None else None
    if window is not None and window.isVisible():
        size = QSize(min(size.width(), window.width() - _WINDOW_MARGIN),
                     min(size.height(), window.height() - _WINDOW_MARGIN))
    least = dialog.minimumSize()
    dialog.setMinimumSize(QSize(min(least.width(), size.width()),
                                min(least.height(), size.height())))
    dialog.resize(size)


def fit_minimum(dialog: QDialog, least: QSize) -> None:
    """``dialog``'s minimum size: ``least``, or more where its content
    needs it to read whole (RG-M4-c 9): its widest row of buttons in the
    words of the language, and the height its wrapping lines take at
    that width. Called again whenever its words change."""
    layout = dialog.layout()
    if layout is None:
        dialog.setMinimumSize(least)
        return
    layout.activate()
    margins = layout.contentsMargins()
    # A row of buttons needs each button at its full width, not the least
    # width Qt would squeeze it to.
    rows = [box.sizeHint().width() for box in dialog.findChildren(QDialogButtonBox)
            if box.isVisibleTo(dialog)]
    width = max(least.width(), layout.minimumSize().width(),
                max(rows, default=0) + margins.left() + margins.right())
    tall = layout.heightForWidth(width) if layout.hasHeightForWidth() else -1
    dialog.setMinimumSize(width, max(least.height(), tall, layout.minimumSize().height()))


# The sheet's margins, and in a window too low for them (the smallest
# window is 189 points tall) the narrower ones it keeps then.
_MARGINS = (20, 18, 20, 14)
_TIGHT_MARGINS = (12, 8, 12, 8)
_TIGHT_HEIGHT = 240


def fit_margins(dialog: QDialog) -> None:
    """The sheet's margins for its height: narrower in a low window, where
    every line counts."""
    margins = _TIGHT_MARGINS if dialog.height() < _TIGHT_HEIGHT else _MARGINS
    layout = dialog.layout()
    if layout is not None and layout.contentsMargins().top() != margins[1]:
        layout.setContentsMargins(*margins)


class _FocusInView(QObject):
    """Keeps the field with the focus in view in a sheet's scrolling part:
    when the focus comes to one (Tab, a click, the sheet opening), and
    when the part is resized under it. QScrollArea scrolls by itself only
    for Tab pressed inside it, so Tab going round from the buttons, and
    the first focus, left the field out of sight in a small window. Of a
    field taller than the part (the note), its caret is shown; a one-line
    field goes by its own place, never by its caret, which Qt places in
    the field only once the field was drawn."""

    def __init__(self, scroll: QScrollArea) -> None:
        super().__init__(scroll)
        self._scroll = scroll
        QApplication.instance().focusChanged.connect(self._focus_moved)
        scroll.viewport().installEventFilter(self)

    @Slot(QWidget, QWidget)
    def _focus_moved(self, _old: Optional[QWidget], now: Optional[QWidget]) -> None:
        self._show(now)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802, Qt's name
        if event.type() == QEvent.Type.Resize:
            self._show(QApplication.focusWidget())
        return False

    def _show(self, widget: Optional[QWidget]) -> None:
        if not shiboken6.isValid(self._scroll) or widget is not None and not shiboken6.isValid(widget):
            return
        page = self._scroll.widget()
        # A field of the page: the page itself with the focus (the handoff)
        # is not brought into view, it stays where it was scrolled to.
        if widget is not None and page is not None and widget is not page \
                and page.isAncestorOf(widget):
            # Margins no larger than the part has room for.
            room = self._scroll.viewport().height()
            margin = max(0, min(8, room // 4))
            if isinstance(widget, QLineEdit):
                # Its caret stands in its middle, but before the field is
                # drawn for the first time Qt says it stands at its top: in
                # a part barely taller than a line the caret was then cut.
                top = widget.mapTo(page, QPoint(0, 0)).y()
                if widget.height() + 2 * margin > room:     # no room for it whole: its middle
                    self._scroll.ensureVisible(0, top + widget.height() // 2, 0, room // 2)
                else:
                    self._scroll.ensureVisible(0, top + widget.height(), 0, margin)
                    self._scroll.ensureVisible(0, top, 0, margin)
            else:
                self._scroll.ensureWidgetVisible(widget, 0, margin)
            # Never sideways: a sheet has no bar to bring the page back with,
            # so a page wider than its part would stay cut at the leading edge.
            self._scroll.horizontalScrollBar().setValue(0)


def scroll_area(page: QWidget) -> QScrollArea:
    """What a sheet holds but its account and buttons: it scrolls, down to
    one line in the smallest window, so they always show whole; the field
    with the focus is always in view (_FocusInView)."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    scroll.setMinimumHeight(scroll.fontMetrics().lineSpacing() + 4)
    scroll.setWidget(page)
    page.setAutoFillBackground(False)
    scroll.viewport().setAutoFillBackground(False)
    _FocusInView(scroll)
    return scroll


# Where a line may break inside a word longer than the line (a German
# compound): after any of its letters, unseen.
_BREAK = "\u200b"


def cut_after_a_word(metrics: QFontMetrics, text: str, width: int, lines: int) -> str:
    """``text`` cut after a word, with "…", to fit ``lines`` lines of
    ``width`` in the font of ``metrics``: the one way a card says a text
    that is too long, on the sheets and in the handoff. A word wider
    than a line (a long compound) may break inside, where the line
    ends, and is cut inside when it is the last that fits: it was drawn
    past the card's edge, and the lines under it cut off. One line that
    a long next word would leave mostly empty takes as much of that
    word as fits."""
    def wide(word: str) -> bool:
        return metrics.horizontalAdvance(word) > width

    def breakable(words: str) -> str:
        return " ".join(_BREAK.join(word) if wide(word) else word
                        for word in words.split(" "))

    room = metrics.lineSpacing() * lines

    def fits(words: str) -> bool:
        return (metrics.boundingRect(0, 0, width, 10_000, Qt.TextFlag.TextWordWrap,
                                     breakable(words)).height() <= room
                and (lines > 1 or not wide(words)))

    if fits(text):
        return breakable(text)
    every = text.split(" ")
    words = list(every)
    while len(words) > 1 and not fits(" ".join(words) + "…"):
        words.pop()
    shown = " ".join(words)
    while shown and not fits(shown + "…"):
        shown = shown[:-1]
    if lines == 1 and len(words) < len(every) \
            and metrics.horizontalAdvance(shown + "…") < width * 2 / 3:
        more = shown + " " + every[len(words)]
        while more and not fits(more + "…"):
            more = more[:-1]
        if len(more) >= len(shown) + 4:             # three letters of it, at the least
            shown = more
    return breakable(shown.rstrip() + "…")


def grow_with_text(edit: QPlainTextEdit, *, least: int, most: int, floor: int = 0) -> None:
    """``edit`` as tall as its text, from ``least`` to ``most`` lines: a
    note of a few lines is read whole, and a scroll bar comes only once
    the field stops growing. Never lower than ``floor`` pixels (a button
    that stands beside it). Called when its text or its width changed."""
    # A plain text's layout counts its height in lines, wrapped ones too.
    counted = round(edit.document().size().height())
    lines = max(least, min(most, counted))
    edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if counted > most
                                    else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    # What the frame and the style take around the text, as drawn.
    margins = edit.contentsMargins()
    around = margins.top() + margins.bottom() + 2 * int(edit.document().documentMargin())
    edit.setFixedHeight(max(floor, lines * edit.fontMetrics().lineSpacing() + around + 2))


def sheet_stylesheet(is_dark: bool) -> str:
    """The app's dialog look (the assistants'), with the chips of people."""
    r = appearance.roles(is_dark)
    border, muted, ring = r.soft, r.muted, r.focus
    return assistant_stylesheet(is_dark) + f"""
    QFrame#person_chip {{ background: {border}; border-radius: 11px; }}
    QLabel#person_role {{ color: {muted}; font-size: 11px; }}
    QToolButton#person_remove {{
        color: {muted}; background: transparent; border: 1px solid transparent;
        border-radius: 8px; padding: 0 4px;
    }}
    QToolButton#person_remove:focus {{ border-color: {ring}; }}
    /* A pop-up button, as the Mac's: the account, then its arrow at the
       trailing end (the arrow stood between the picture and the name). */
    QToolButton#profile_switch {{
        background: {r.soft}; color: {r.ink}; border: 1px solid {r.edge}; border-radius: 14px;
        padding: 3px {_ACCOUNT_ARROW_END + 9}px 3px 5px;
    }}
    QToolButton#profile_switch:hover {{ background: {border}; }}
    QToolButton#profile_switch:focus {{ border-color: {ring}; }}
    QToolButton#profile_switch::menu-indicator {{ image: none; width: 0; }}
    QLabel#article_cover_preview {{
        border: 1px solid {border}; border-radius: 4px; color: {muted}; font-size: 11px;
    }}
    /* The scrolling part's bar lies on the sheet, with no band of its own. */
    QScrollBar:vertical {{ background: transparent; }}
    """ + appearance.disclosure_stylesheet(r)


def people_tagged(event: dict, content: str, *, markdown: bool) -> List[Tuple[str, str]]:
    """``(pubkey, role)`` for each person ``event`` tags, in its order:
    a co-author (the author marker), someone the text mentions, or the
    author of something it quotes. The signer's own author tag, written
    first when there are co-authors, is not one of them."""
    mentioned = {pubkey for pubkey, _hint in extract_inline_mentions(content,
                                                                      markdown=markdown)}
    signer = event.get("pubkey", "")
    people = []
    for tag in event.get("tags", ()):
        if tag[:1] != ["p"] or len(tag) < 2:
            continue
        author = len(tag) >= 4 and tag[3] == AUTHOR_MARKER
        if author and tag[1] == signer:
            continue
        role = COAUTHOR if author else MENTIONED if tag[1] in mentioned else QUOTED
        people.append((tag[1], role))
    return people


class _Person(QFrame):
    """One person: avatar, name and how they are tagged; × for a co-author."""

    def __init__(self, pubkey: str, name: str, role: str, avatar,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.pubkey = pubkey
        self.setObjectName("person_chip")
        words = {MENTIONED: _("mentioned"), QUOTED: _("quoted"),
                 COAUTHOR: _("co-author")}[role]
        self.setAccessibleName(_("{name}, {role}").format(name=name, role=words))
        try:
            address = encode_npub(pubkey)
        except (ValueError, TypeError):
            address = pubkey
        self.setToolTip(f"{name}\n{address}")
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 2, 8 if role != COAUTHOR else 2, 2)
        row.setSpacing(5)
        self.avatar = QLabel()
        self.avatar.setFixedSize(_PERSON_AVATAR, _PERSON_AVATAR)
        row.addWidget(self.avatar)
        self.set_avatar(avatar, name)
        label = QLabel(name)
        label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(label)
        role_label = QLabel(words)
        role_label.setObjectName("person_role")
        row.addWidget(role_label)
        self.remove: Optional[QToolButton] = None
        if role == COAUTHOR:
            self.remove = QToolButton()
            self.remove.setObjectName("person_remove")
            self.remove.setText("×")
            remove = _("Remove {name}").format(name=name)
            self.remove.setAccessibleName(remove)
            self.remove.setToolTip(remove)
            self.remove.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(self.remove)

    def set_avatar(self, picture, name: str) -> None:
        self.avatar.setPixmap(
            make_avatar_pixmap_from_image(picture, size=_PERSON_AVATAR)
            if picture is not None and not picture.isNull()
            else pixmap_for_profile(name, self.pubkey, None, size=_PERSON_AVATAR))

    def mousePressEvent(self, event) -> None:  # noqa: N802, Qt's name
        # Who it is, at a click too: their name and Nostr address.
        QToolTip.showText(event.globalPosition().toPoint(), self.toolTip(), self)
        super().mousePressEvent(event)


class PeopleRow(QWidget):
    """Everyone tagged (see the module docstring). ``remove_requested`` with
    a co-author's pubkey when their × is chosen."""

    remove_requested = Signal(str)

    def __init__(self, known_people: KnownPeople, avatars: AvatarStore, *,
                 add_button: Optional[QPushButton] = None, nobody: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._known_people = known_people
        self._avatars = avatars
        self._people: List[_Person] = []
        self._shown: Optional[List[Tuple[str, str]]] = None
        self._flow = FlowLayout(self, spacing=6)
        self._nobody = QLabel(nobody)
        self._nobody.setObjectName("muted")
        self._nobody.setWordWrap(True)
        self._flow.addWidget(self._nobody)
        self._add_button = add_button
        if add_button is not None:
            self._flow.addWidget(add_button)
        avatars.avatar_added.connect(self._on_avatar_added)
        self.show_people(())

    def people(self) -> List[Tuple[str, str]]:
        """``(pubkey, accessible name)`` of each person shown."""
        return [(person.pubkey, person.accessibleName()) for person in self._people]

    def show_people(self, people: Iterable[Tuple[str, str]]) -> None:
        """Show ``people``, ``(pubkey, role)`` each, in order. The same
        people as shown already change nothing (a note asks at every key
        typed)."""
        people = list(people)
        if people == self._shown:
            return
        self._shown = people
        for person in self._people:
            self._flow.removeWidget(person)
            person.hide()
            person.deleteLater()
        self._flow.removeWidget(self._nobody)
        if self._add_button is not None:
            self._flow.removeWidget(self._add_button)
        self._people = []
        for pubkey, role in people:
            name = self._known_people.name_of(pubkey)
            person = _Person(pubkey, name, role, self._avatars.get(pubkey), self)
            if person.remove is not None:
                person.remove.clicked.connect(
                    lambda _checked=False, key=pubkey: self.remove_requested.emit(key))
            self._flow.addWidget(person)
            person.show()
            self._people.append(person)
        self._nobody.setVisible(not self._people and bool(self._nobody.text()))
        self._flow.addWidget(self._nobody)
        if self._add_button is None:
            return              # nobody to remove: a note tags no co-author
        self._flow.addWidget(self._add_button)
        # Tab goes left to right: each ×, then Add Co-Author… (each × follows
        # the button first, where Tab already goes; then the button moves
        # after the last of them).
        previous = self._add_button
        for person in self._people:
            if person.remove is not None:
                QWidget.setTabOrder(previous, person.remove)
                previous = person.remove
        if previous is not self._add_button:
            QWidget.setTabOrder(previous, self._add_button)

    def _on_avatar_added(self, pubkey: str, picture) -> None:
        for person in self._people:
            if person.pubkey == pubkey:
                person.set_avatar(picture, self._known_people.name_of(pubkey))


class AccountRow(QWidget):
    """"Publishing as" (or ``label``, with ``accessible`` naming the
    button, ``{name}`` the account's) and the account, with a menu of the
    others. ``switched(profile)`` when another is chosen; it becomes the
    account in use, as at the window's own account menu."""

    switched = Signal(object)

    def __init__(self, profile: Profile, store: ProfileStore, avatars: AvatarStore, *,
                 is_dark: bool, label: str = "", accessible: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._accessible = accessible or _("Publishing as {name}")
        self._profile = profile
        self._store = store
        self._avatars = avatars
        self._is_dark = is_dark
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(QLabel(label or _("Publishing as")))
        self.button = QToolButton()
        self.button.setObjectName("profile_switch")
        self.button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setIconSize(QSize(_ACCOUNT_AVATAR, _ACCOUNT_AVATAR))
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu = QMenu(self.button)
        self.button.setMenu(self.menu)
        row.addWidget(self.button)
        avatars.avatar_added.connect(self._on_avatar_added)
        self._show()

    def profile(self) -> Profile:
        return self._profile

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._show()

    def _show(self) -> None:
        profile = self._profile
        avatar = pixmap_for_profile(profile.display_name, profile.user_pubkey,
                                    self._avatars.get(profile.user_pubkey),
                                    size=_ACCOUNT_AVATAR)
        self.button.setIcon(QIcon(avatar))
        appearance.menu_arrow(self.button, self._is_dark, end=_ACCOUNT_ARROW_END)
        name = profile.display_name or profile.npub_short()
        self.button.setText(f" {name}")
        self.button.setToolTip(profile.npub_short())
        self.button.setAccessibleName(self._accessible.format(name=name))
        self.menu.clear()
        profiles = self._store.list()
        if not profiles:
            self.menu.addAction(_("(no accounts)")).setEnabled(False)
            return
        for other in profiles:
            action = self.menu.addAction(other.display_name or other.npub_short())
            action.setCheckable(True)
            action.setChecked(other.user_pubkey == profile.user_pubkey)
            action.triggered.connect(lambda _checked=False, p=other: self._switch(p))

    def _switch(self, profile: Profile) -> None:
        if profile.user_pubkey == self._profile.user_pubkey:
            return
        self._profile = profile
        self._store.set_default(profile.user_pubkey)
        self._show()
        self.switched.emit(profile)

    def _on_avatar_added(self, pubkey: str, _picture) -> None:
        if pubkey == self._profile.user_pubkey:
            self._show()


class StatusLine(QLabel):
    """What is going on, or what went wrong, above the buttons. A total
    failure has a ``title`` too, what did not happen, shown above its
    sentence in bold and said once (lessons 36)."""

    def __init__(self, is_dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_dark = is_dark
        self._error = False
        # What is shown: (its title, its words), plain.
        self.shown = ("", "")
        self.setWordWrap(True)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setVisible(False)

    def say(self, text: str, *, error: bool = False, title: str = "",
            detail: str = "", quietly: bool = False) -> None:
        """Show ``text`` (with its ``title``, for a total failure); the
        technical ``detail`` only in the tooltip (lessons 26 and 36).
        ``quietly``: shown and not announced (a wait that is over)."""
        self.setToolTip(detail)
        if title:
            self.setTextFormat(Qt.TextFormat.RichText)
            self.setText(markup(title, text))
        else:
            self.setTextFormat(Qt.TextFormat.PlainText)
            self.setText(text)
        self.setVisible(bool(text))
        self._error = error
        self._color()
        if text and (error or title) and (title, text) != self.shown and not quietly:
            announce.announce(self, spoken(title, text))
        self.shown = (title, text)

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self._color()

    def _color(self) -> None:
        # A reason in the sheet's own secondary ink, as its hints are: in
        # the app's grey it was the dimmest text on a dark sheet, though it
        # says why the default button is dimmed.
        roles = appearance.roles(self._is_dark)
        self.setStyleSheet(f"color: {roles.error if self._error else roles.muted};")


class BottomRow(QWidget):
    """The account and the buttons (see the module docstring)."""

    def __init__(self, account: AccountRow, buttons: QDialogButtonBox,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._account = account
        self._buttons = buttons
        self._row = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(8)
        self._row.addWidget(account, 0, Qt.AlignmentFlag.AlignLeft)
        self._row.addStretch(1)
        self._row.addWidget(buttons, 0, Qt.AlignmentFlag.AlignRight)
        # Tab goes left to right: the account, then the buttons in the order
        # the platform puts them in (Cancel first on a Mac and in GNOME),
        # known before they are placed. (Never asked of the box's own layout
        # items: wrapping those leaves Python holding items the box deletes.)
        layout = buttons.style().styleHint(QStyle.StyleHint.SH_DialogButtonLayout, None,
                                           buttons)
        reject_first = layout in (QDialogButtonBox.ButtonLayout.MacLayout.value,
                                  QDialogButtonBox.ButtonLayout.GnomeLayout.value)
        accepts = [b for b in buttons.buttons()
                   if buttons.buttonRole(b) != QDialogButtonBox.ButtonRole.RejectRole]
        rejects = [b for b in buttons.buttons()
                   if buttons.buttonRole(b) == QDialogButtonBox.ButtonRole.RejectRole]
        previous = account.button
        for button in (rejects + accepts if reject_first else accepts + rejects):
            QWidget.setTabOrder(previous, button)
            previous = button

    def resizeEvent(self, event) -> None:  # noqa: N802, Qt's name
        super().resizeEvent(event)
        beside = (self._account.sizeHint().width() + self._row.spacing()
                  + self._buttons.sizeHint().width())
        direction = (QBoxLayout.Direction.LeftToRight if self.width() >= beside
                     else QBoxLayout.Direction.TopToBottom)
        if self._row.direction() != direction:
            self._row.setDirection(direction)
