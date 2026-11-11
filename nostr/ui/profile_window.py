# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Edit Profile: the name, picture and addresses people see for an account.

Three pages, in the order they happen:

    reading   the profile is read fresh from the network
    form      the fields, with any problem shown under its field
    saving    the change is signed and published

A profile is one replaceable event: what is published last is the whole
profile, in every app. So the window never edits from a remembered copy.
It reads the profile first, and only a profile that was found (or one the
network clearly does not have) can be edited; when it cannot be read, the
window says so and offers to try again rather than risk replacing it.
Saving sends only the fields that were changed, and the writer
(nostr/outbox/writer.py, update_profile) reads the profile once more and
keeps every field this window does not show (banner colors, bots' flags,
anything another app added).

The form opens with the profile as others will see it (profile_card.py):
the banner, the picture, the name and the address, following the fields as
they change. The fields come in three groups, as people think of them:
who they are (name, username, about: under the card, untitled), how they
look (picture and banner,
each chosen from the Media Library with Choose…, which also uploads, or
given by its address), and where else to find and pay them.

The window does no network work itself: ``read`` and ``save`` are handed
in (nostr/profile_editing.py has the real ones), and so are ``media`` (the
Media Library's services, for Choose…) and ``load_image`` (which fetches
the card's pictures), which is also what lets the tests drive it.
"""

from __future__ import annotations

import json
import re
import weakref
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
    QWidget,
)

import announce
from i18n import N_, _, ngettext
from nostr import failures, refs
from nostr.outbox.policy import LookupState
from nostr.outbox.writer import UNCHANGED, WRITTEN, failure_detail, failure_words
from nostr.ui.assistant import (
    DEFAULT, NORMAL, AssistantWindow, busy_bar, page, text_label,
)
from nostr.ui.cover_field import MediaServices, choose_picture
from nostr.ui.profile_card import ProfileCard
from nostr.ui.publish_copy_dialog import resolve_pick

READING = "reading"
FORM = "form"
SAVING = "saving"
PROBLEM = "problem"

# read(on_done(Lookup)) and save(changes, on_done(WriteOutcome)).
ReadFn = Callable[[Callable[[object], None]], None]
SaveFn = Callable[[Dict[str, str], Callable[[object], None]], None]
# load_image(kind, url, on_done(QPixmap or None)): "picture" or "banner".
ImageFn = Callable[[str, str, Callable[[Optional[QPixmap]], None]], None]

# How long the card waits after an address changes before it loads the
# picture, so typing one does not load each half-typed address.
IMAGE_DELAY_MS = 600


@dataclass(frozen=True)
class Field:
    key: str                # the profile's own name for it (NIP-01, NIP-24, NIP-57)
    label: str
    placeholder: str = ""
    kind: str = "text"      # text, about, link, image, lightning, address


# In the order people think of them: who they are, how they look, where
# else to find them. The labels are marked here and translated where shown;
# they end in a colon, as every form's labels in MyEditor do, and a screen
# reader gets them without it.
FIELDS = (
    Field("display_name", N_("Name:"), N_("The name people see")),
    Field("name", N_("Username:"), N_("A short name, without spaces")),
    Field("about", N_("About:"), kind="about"),
    Field("picture", N_("Picture:"), N_("Address of an image, https://…"), kind="image"),
    Field("banner", N_("Banner:"), N_("Address of a wide image, https://…"), kind="image"),
    Field("website", N_("Website:"), "https://…", kind="link"),
    Field("lud16", N_("Lightning address:"), N_("you@wallet.example"), kind="lightning"),
    Field("nip05", N_("Nostr address:"), N_("you@example.com"), kind="address"),
)

_IMAGE_FIELD = next(field for field in FIELDS if field.kind == "image")

# The groups the fields come in after the first (name, username, about,
# which the card above introduces), each titled before its first field.
SECTIONS = {
    "picture": N_("Pictures"),
    "website": N_("Links and Payments"),
}

# What Choose… beside a picture field is called, and its picker's title.
CHOOSE_TITLES = {"picture": N_("Choose Picture"), "banner": N_("Choose Banner")}

# Older apps wrote the display name under this key; it is shown when the
# newer one is missing.
_LEGACY_DISPLAY_NAME = "displayName"

_ADDRESS = re.compile(r"\A(?:[A-Za-z0-9._+-]+@)?[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
_LIGHTNING = re.compile(r"\A[A-Za-z0-9._+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
_WEB = re.compile(r"\Ahttps?://[^\s/?#]+\.[^\s/?#]+(?:[/?#]\S*)?\Z", re.IGNORECASE)
_SECURE_WEB = re.compile(r"\Ahttps://[^\s/?#]+\.[^\s/?#]+(?:[/?#]\S*)?\Z", re.IGNORECASE)


def field_problem(field: Field, value: str) -> Optional[str]:
    """What is wrong with ``value`` for ``field``, in plain words, or None.
    An empty field is never a problem: it removes that part of the profile."""
    value = value.strip()
    if not value:
        return None
    if refs.secret_keys_in(value):
        return _("This holds a secret key. Anyone who has it can publish as its owner, so "
                 "it never goes into a profile. Remove it.")
    if field.kind == "image" and not _SECURE_WEB.match(value):
        return _("Enter an image address beginning with https, like "
                 "https://example.com/picture.jpg.")
    if field.kind == "link" and not _WEB.match(value):
        return _("Enter a web address beginning with http or https, like "
                 "https://example.com.")
    if field.kind == "lightning" and not _LIGHTNING.match(value):
        return _("Enter a Lightning address like you@wallet.example.")
    if field.kind == "address" and not _ADDRESS.match(value):
        return _("Enter a Nostr address like you@example.com.")
    if field.key == "name" and any(c.isspace() for c in value):
        return _("A username can’t contain spaces.")
    return None


def profile_fields(event: Optional[dict]) -> Dict[str, str]:
    """The window's fields from a profile event; raises ValueError for a
    profile that is not a JSON object (it is never guessed at)."""
    if event is None:
        return {f.key: "" for f in FIELDS}
    data = json.loads(event.get("content") or "{}")
    if not isinstance(data, dict):
        raise ValueError("the profile is not a JSON object")
    values = {}
    for field in FIELDS:
        value = data.get(field.key)
        if field.key == "display_name" and not value:
            value = data.get(_LEGACY_DISPLAY_NAME)
        values[field.key] = value.strip() if isinstance(value, str) else ""
    return values


def hidden_secret_fields(event: Optional[dict]) -> List[str]:
    """The parts of a profile this window doesn't show (other apps' own,
    or the older display name key) that hold a Nostr secret key. They are
    public already; saving without them takes the key out of the profile,
    and a profile with them would not be signed."""
    if event is None:
        return []
    data = json.loads(event.get("content") or "{}")
    shown = {field.key for field in FIELDS}
    return [key for key, value in data.items() if key not in shown
            and refs.secret_keys_in(value if isinstance(value, str) else json.dumps(value))]


class ProfileWindow(AssistantWindow):
    """Edit Profile, for the active account."""

    saved = Signal()        # the changed profile reached the relays

    def __init__(self, *, read: ReadFn, save: SaveFn, signs_locally: bool = False,
                 media: Optional[MediaServices] = None,
                 load_image: Optional[ImageFn] = None,
                 is_dark: bool = True, parent=None) -> None:
        super().__init__(_("Edit Profile"), is_dark=is_dark, parent=parent)
        self._read = read
        self._save = save
        self._media = media
        self._load_image = load_image
        self._is_dark = is_dark
        # The address each picture of the card was last asked for: an answer
        # for another one (typed over since) is dropped.
        self._shown_images: Dict[str, str] = {"picture": "", "banner": ""}
        self._signs_locally = signs_locally
        self._loaded: Dict[str, str] = {}
        # Parts the window doesn't show that hold a secret key: left out.
        self._removing: List[str] = []
        self._new_profile = False
        self._closed = False
        self._edits: Dict[str, QWidget] = {}
        self._errors: Dict[str, QLabel] = {}
        self._choose: Dict[str, QPushButton] = {}
        self._image_timer = QTimer(self)
        self._image_timer.setSingleShot(True)
        self._image_timer.setInterval(IMAGE_DELAY_MS)
        self._image_timer.timeout.connect(self._load_card_images)
        self._build_reading()
        self._build_form()
        self._build_saving()
        self._build_problem()
        self.start_reading()

    # -- reading -------------------------------------------------------------

    def _build_reading(self) -> None:
        body, col = page()
        col.addStretch(1)
        self._reading_label = text_label(_("Reading your profile…"), "item_title")
        col.addWidget(self._reading_label)
        col.addWidget(busy_bar())
        col.addStretch(2)
        self.add_page(READING, body)

    def start_reading(self) -> None:
        self.show_page(READING, [("cancel", _("Cancel"), DEFAULT, self.reject)])
        self._read(self._on_read)

    def _on_read(self, result) -> None:
        if self._closed:
            return
        if result.state is LookupState.UNKNOWN:
            # Why it is not edited unread is the tooltip (lessons 36).
            self._show_problem(
                _("Your Profile Couldn’t Be Read"), _(failures.CHECK_CONNECTION), retry=True,
                detail=_("Editing your profile without reading it first could replace it."))
            return
        try:
            self._loaded = profile_fields(result.event)
            self._removing = hidden_secret_fields(result.event)
        except ValueError:
            self._show_problem(
                _("This Profile Can’t Be Edited Here"),
                _("Your profile is stored in a form MyEditor doesn’t understand: edit it in "
                  "the app that created it."),
                retry=False)
            return
        self._new_profile = result.state is LookupState.ABSENT
        self._show_form()

    # -- the form ------------------------------------------------------------

    def _build_form(self) -> None:
        body, col = page()
        col.addWidget(text_label(_("Your Profile"), "title"))
        self._form_note = text_label("", "muted")
        col.addWidget(self._form_note)
        self.card = ProfileCard(is_dark=self._is_dark)
        col.addWidget(self.card)
        form = QFormLayout()
        form.setSpacing(8)
        # Labels right-aligned beside their fields, as in every form here.
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # A text box still reports a change while it is being destroyed;
        # through a weak reference that report never reaches a window that
        # is already going.
        window = weakref.ref(self)

        def edited(*_args) -> None:
            alive = window()
            if alive is not None and not alive._closed:
                alive._on_edited()

        for field in FIELDS:
            if field.key in SECTIONS:
                heading = text_label(_(SECTIONS[field.key]), "item_title")
                heading.setContentsMargins(0, 4, 0, 0)
                form.addRow(heading)
            if field.kind == "about":
                edit = QPlainTextEdit()
                edit.setFixedHeight(84)
            else:
                edit = QLineEdit()
                edit.setPlaceholderText(_(field.placeholder) if field.placeholder else "")
            edit.textChanged.connect(edited)
            edit.setAccessibleName(_(field.label).rstrip(":").strip())
            error = text_label("", "error")
            error.hide()
            holder = QWidget()
            stack = QVBoxLayout(holder)
            stack.setContentsMargins(0, 0, 0, 0)
            stack.setSpacing(2)
            if field.kind == "image" and self._media is not None:
                stack.addLayout(self._with_choose(field.key, edit))
            else:
                stack.addWidget(edit)
            stack.addWidget(error)
            form.addRow(_(field.label), holder)
            self._edits[field.key] = edit
            self._errors[field.key] = error
        col.addLayout(form)
        col.addStretch(1)
        self.add_page(FORM, body)

    def _with_choose(self, key: str, edit: QWidget) -> QHBoxLayout:
        """The address beside Choose…, which opens the Media Library to pick
        a picture or upload one."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(edit, 1)
        button = QPushButton(_("Choose…"))
        button.setAccessibleName(_(CHOOSE_TITLES[key]))
        button.setToolTip(_("Choose a picture from your Media Library, or upload one."))
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False, k=key: self._choose_picture(k))
        row.addWidget(button)
        self._choose[key] = button
        return row

    def _choose_picture(self, key: str) -> None:
        choose_picture(self._media, title=_(CHOOSE_TITLES[key]), is_dark=self._is_dark,
                       parent=self, on_picked=lambda media, k=key: self._picked(k, media))

    def _picked(self, key: str, media) -> None:
        """A picture chosen for ``key``: its public address, or why not, said
        under the field (a private one gets a public copy first, or stays)."""
        resolved = resolve_pick(media, visibility=self._media.visibility,
                                maker=self._media.copy_maker, is_dark=self._is_dark,
                                parent=self)
        if not resolved.ok:
            if resolved.reason:
                announce.field_problem(self._edits[key], self._errors[key], resolved.reason)
            return
        edit = self._edits[key]
        edit.setText(resolved.url)
        edit.setCursorPosition(0)

    def _update_card(self) -> None:
        """The card as the fields read now; its pictures shortly after an
        address stops changing."""
        name = self._value("display_name") or self._value("name")
        username = self._value("name")
        detail = self._value("nip05") or (f"@{username}" if username else "")
        self.card.set_text(name, detail)
        if any(self._value(kind) != shown for kind, shown in self._shown_images.items()):
            self._image_timer.start()

    def _load_card_images(self) -> None:
        for kind, shown in self._shown_images.items():
            url = self._value(kind)
            if url == shown:
                continue
            self._shown_images[kind] = url
            setter = self.card.set_picture if kind == "picture" else self.card.set_banner
            if not url or self._load_image is None or field_problem(_IMAGE_FIELD, url):
                setter(None)
                continue
            window = weakref.ref(self)

            def landed(pixmap, kind=kind, url=url) -> None:
                alive = window()
                if alive is not None and not alive._closed and alive._shown_images[kind] == url:
                    (alive.card.set_picture if kind == "picture"
                     else alive.card.set_banner)(pixmap)

            self._load_image(kind, url, landed)

    def _value(self, key: str) -> str:
        edit = self._edits[key]
        text = edit.toPlainText() if isinstance(edit, QPlainTextEdit) else edit.text()
        return text.strip()

    def _show_form(self) -> None:
        for field in FIELDS:
            edit = self._edits[field.key]
            edit.blockSignals(True)
            if isinstance(edit, QPlainTextEdit):
                edit.setPlainText(self._loaded.get(field.key, ""))
            else:
                edit.setText(self._loaded.get(field.key, ""))
            if isinstance(edit, QLineEdit):
                # A long address shows where it lives, not its last letters.
                edit.setCursorPosition(0)
                edit.setToolTip(edit.text())
            edit.blockSignals(False)
            self._errors[field.key].hide()
        if self._removing:
            names = ", ".join(_("“{part}”").format(part=key) for key in self._removing)
            self._set_form_note(ngettext(
                "Your profile has a secret key in {names}, a part this window doesn’t show. "
                "Saving leaves that part out, which takes the key out of your profile.",
                "Your profile has a secret key in {names}, parts this window doesn’t show. "
                "Saving leaves those parts out, which takes the key out of your profile.",
                len(self._removing)).format(names=names), problem=True)
            announce.announce(self._form_note, self._form_note.text())
        else:
            self._set_form_note(
                _("You don’t have a profile yet. What you enter here becomes your profile.")
                if self._new_profile else
                _("This is what people see in every Nostr app. An empty field is removed "
                  "from your profile."))
        self._show_form_page()

    def _show_form_page(self, *, again: bool = False, save: bool = True) -> None:
        """The form; after a save that did not work, its Save reads Try
        Again (``again``), or is not offered when no try here can help."""
        buttons = [("cancel", _("Cancel"), NORMAL if save else DEFAULT, self.reject)]
        if save:
            buttons.append(("save", _("Try Again") if again else _("Save"), DEFAULT,
                            self._save_changes))
        self.show_page(FORM, buttons)
        self._on_edited()

    def _set_form_note(self, text: str, *, problem: bool = False, title: str = "",
                       detail: str = "") -> None:
        """The note above the form; a ``problem`` with a ``title`` is a
        total failure: the title in bold above its one sentence, what the
        relays and the signer said in the tooltip (lessons 36)."""
        self._form_note.setObjectName("error" if problem else "muted")
        self._form_note.style().unpolish(self._form_note)
        self._form_note.style().polish(self._form_note)
        self._form_note.setTextFormat(Qt.RichText if title else Qt.PlainText)
        self._form_note.setText(failures.markup(title, text) if title else text)
        self._form_note.setAccessibleName(failures.spoken(title, text) if title else "")
        self._form_note.setToolTip(detail)
        if title:
            announce.announce(self._form_note, failures.spoken(title, text))

    def changes(self) -> Dict[str, str]:
        """The fields that differ from the profile as it was read, and the
        parts holding a secret key, left out (an empty value removes)."""
        changes = {key: "" for key in self._removing}
        changes.update({field.key: self._value(field.key) for field in FIELDS
                        if self._value(field.key) != self._loaded.get(field.key, "")})
        return changes

    def _on_edited(self) -> None:
        problems = False
        for field in FIELDS:
            problem = field_problem(field, self._value(field.key))
            announce.field_problem(self._edits[field.key], self._errors[field.key], problem or "")
            problems = problems or bool(problem)
        button = self.buttons.get("save")
        if button is not None:
            button.setEnabled(bool(self.changes()) and not problems)
        self._update_card()

    # -- saving --------------------------------------------------------------

    def _build_saving(self) -> None:
        body, col = page()
        col.addStretch(1)
        self._saving_label = text_label("", "item_title")
        col.addWidget(self._saving_label)
        col.addWidget(busy_bar())
        col.addStretch(2)
        self.add_page(SAVING, body)

    def _save_changes(self) -> None:
        changes = self.changes()
        if not changes:
            return
        text = _("Saving your profile…")
        if not self._signs_locally:
            text += " " + _("Your signer app may ask you to approve.")
        self._saving_label.setText(text)
        self.show_page(SAVING, [])
        self._save(changes, self._on_saved)

    def _on_saved(self, outcome) -> None:
        if self._closed:
            return
        if outcome.status in (WRITTEN, UNCHANGED):
            self.saved.emit()
            self.accept()
            return
        # Back to the form, with what was typed, and what did not happen
        # above it, said once with its cause (lessons 36).
        title = _("Your profile wasn’t saved")
        if outcome.reason == _(refs.SECRET_KEY_REFUSED):
            # A key arrived since it was read, in a part this window doesn't
            # show (its own fields are checked): no retry here helps.
            self._set_form_note(
                _("Since it was read, a secret key was added to a part this window doesn’t "
                  "show: close this window and open Edit Profile again to leave that part "
                  "out."),
                problem=True, title=title)
            self._show_form_page(save=False)
            return
        self._set_form_note(failure_words(outcome), problem=True, title=title,
                            detail=failure_detail(outcome))
        self._show_form_page(again=True)

    # -- problems ------------------------------------------------------------

    def _build_problem(self) -> None:
        body, col = page()
        self._problem_title = text_label("", "title")
        self._problem_text = text_label("")
        col.addWidget(self._problem_title)
        col.addWidget(self._problem_text)
        col.addStretch(1)
        self.add_page(PROBLEM, body)

    def _show_problem(self, title: str, text: str, *, retry: bool, detail: str = "") -> None:
        self._problem_title.setText(title)
        self._problem_text.setText(text)
        self._problem_text.setToolTip(detail)
        if retry:
            buttons = [("close", _("Close"), NORMAL, self.reject),
                       ("retry", _("Try Again"), DEFAULT, self.start_reading)]
        else:
            buttons = [("close", _("Close"), DEFAULT, self.reject)]
        self.show_page(PROBLEM, buttons)

    def done(self, result: int) -> None:
        # An answer that arrives after the window closed is dropped.
        self._closed = True
        self._image_timer.stop()
        super().done(result)
