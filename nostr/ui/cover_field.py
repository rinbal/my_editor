# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An article's cover image: a picture well, Choose…, Remove, and the
cover's web address. One field for Publish as Article and Article
Details, so a cover is chosen the same way in both.

In the publish window the well leads, with its buttons beside it; the
address is the window's to place (under Show Details):

    [ picture ]   Choose…
                  Remove

In the narrow inspector everything stacks, as Pages stacks its controls:
the well across the column in the article card's 16:9 framing, then
Choose and Remove, then the address as a line of its own:

    [ picture                ]
    Choose ▾   Adjust…   Remove
    cdn.example/cover.jpg

There the well is an image well as Apple's guidelines describe one: it
takes focus, a picture file dropped on it, and Paste (a picture file or
a picture); Delete removes the cover, Copy copies its address, Return
opens the Media Library. Choose is a pull-down: the Media Library, or a
web address typed in the field it shows.

Choose… is the Media Library's picker, which also uploads. A picture
kept private cannot be a cover as it stands, since its bytes are
ciphertext: choosing one makes a public copy first
(publish_copy_dialog.resolve_pick), or leaves the cover as it was and
says why. A picture file dropped on the well is uploaded to the media
servers as a public picture, with what it reveals besides its pixels
(where and when it was taken) taken out first (media/scrub.py), and
becomes the cover once it is there. Remove takes the cover away.

The well uses the article card's wide framing and shows the source's
dimensions, with guidance for a 16:9 cover at 1600 × 900 pixels. Adjust…
opens a live crop: drag or use arrow keys to choose the subject's position,
zoom, and reset. Applying uploads a separate, metadata-free still cover;
the original is kept. The cover URL changes only when that upload succeeds.

The well shows the picture of a cover from the account's media library;
an address typed or pasted shows no preview, since downloading any
address typed would be a surprise.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QGuiApplication, QKeySequence, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

import media_files
from . import appearance
from i18n import _

from ..blossom.hashes import hash_from_url
from ..blossom.store import MediaFile, MediaStore
from ..media.media_visibility import MediaVisibility
from ..media.private_library import PrivateLibrary
from ..media.publish_copy import PublicCopyMaker
from ..media.scrub import ScrubError, scrub_for_publication
from ..profiles import Profile
from .media_library_dialog import MediaLibraryDialog
from .eliding_label import ElidingLabel
from .media_saver import MediaSaver
from .publish_copy_dialog import resolve_pick
from .cover_crop_dialog import CoverCropDialog
from .flow_layout import FlowLayout
from .thumbnail_loader import ThumbnailLoader



def library_picture_hash(media_store: Optional[MediaStore], address: str) -> str:
    """The hash of the picture behind ``address`` when it is one of the
    account's media library, whose own copy can be shown; "" otherwise.
    An address typed is never downloaded to show it: that would be a
    surprise (and a request to a server nobody chose)."""
    sha = (hash_from_url(address.strip()) or "").lower() if address.strip() else ""
    return sha if sha and media_store is not None and sha in media_store.files else ""


THUMB = QSize(144, 81)             # the same 16:9 framing as the article card


@dataclass(frozen=True)
class MediaServices:
    """What choosing a picture from the Media Library needs: the account's
    media servers, and what tells a private file from a public one and
    makes a public copy of it."""
    store: MediaStore
    visibility: Optional[MediaVisibility] = None
    copy_maker: Optional[PublicCopyMaker] = None
    private_library: Optional[PrivateLibrary] = None
    profile: Callable[[], Optional[Profile]] = lambda: None
    saver: Optional[MediaSaver] = None


def choose_picture(services: MediaServices, *, title: str, is_dark: bool, parent: QWidget,
                   on_picked: Callable[[MediaFile], None]) -> None:
    """The Media Library's picker, showing pictures, without the
    alternative text row: ``on_picked(media)`` for the file chosen (its
    Add… uploads one first). A private one is the caller's to resolve
    (publish_copy_dialog.resolve_pick)."""
    # Reading the private library is what lets the picker tell a private
    # file from a public one; it starts here, when someone goes looking for
    # a picture, not on the way into the window.
    if services.private_library is not None:
        services.private_library.bind_profile(services.profile())
    picker = MediaLibraryDialog(store=services.store, is_dark=is_dark,
                                pick_mode=True, pick_alt_text=False,
                                visibility=services.visibility or MediaVisibility(),
                                saver=services.saver, parent=parent)
    if services.private_library is not None:
        picker.bind_private_library(services.private_library)
    picker.setWindowTitle(title)
    # Pictures only: audio and video can't be one.
    picker._filter_combo.setCurrentIndex(1)
    picker.file_picked.connect(on_picked)
    picker.exec()


class _Well(QLabel):
    """The picture, where a picture file can be dropped. Its picture is
    shown filling the well, cropped from the middle; words in its place
    while there is none. ``wide``: across its column at 16:9, an image well
    with focus, Paste, Copy, Delete and Return (see the module)."""

    dropped = Signal(str)
    cleared = Signal()
    activated = Signal()
    copied = Signal()

    RADIUS = 6

    def __init__(self, parent: Optional[QWidget] = None, *, wide: bool = False) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._picture = QPixmap()
        self._wide = wide
        if wide:
            # Its height follows its width (16:9), which a layout asks only
            # when the size policy says so.
            policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def setPixmap(self, picture: QPixmap) -> None:  # noqa: N802, Qt's name
        self._picture = QPixmap(picture)
        self.update()

    def hasHeightForWidth(self) -> bool:  # noqa: N802, Qt's name
        return self._wide

    def heightForWidth(self, width: int) -> int:  # noqa: N802, Qt's name
        # The article card's 16:9 framing, as the small well (THUMB).
        return width * 9 // 16 if self._wide else super().heightForWidth(width)

    def sizeHint(self) -> QSize:  # noqa: N802, Qt's name
        return QSize(240, 135) if self._wide else super().sizeHint()

    def pixmap(self) -> QPixmap:  # noqa: D102, Qt's name
        """The picture as the well shows it: filling its frame, cropped from
        the middle; null when it shows words."""
        if self._picture.isNull():
            return QPixmap()
        frame = self.contentsRect().size()
        scaled = self._picture.scaled(frame, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                      Qt.TransformationMode.SmoothTransformation)
        return scaled.copy((scaled.width() - frame.width()) // 2,
                           (scaled.height() - frame.height()) // 2,
                           frame.width(), frame.height())

    def paintEvent(self, event) -> None:  # noqa: N802, Qt's name
        if self._picture.isNull():
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = self.contentsRect()
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(inner), self.RADIUS, self.RADIUS)
        painter.setClipPath(shape)
        painter.drawPixmap(inner.topLeft(), self.pixmap())

    def keyPressEvent(self, event) -> None:  # noqa: N802, Qt's name
        if self._wide and self.isEnabled():
            if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.cleared.emit()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                self.activated.emit()
                return
            if event.matches(QKeySequence.StandardKey.Paste):
                self._paste()
                return
        if self._wide and event.matches(QKeySequence.StandardKey.Copy):
            self.copied.emit()
            return
        super().keyPressEvent(event)

    def _paste(self) -> None:
        """A picture file copied in the Finder or Explorer, or a picture
        itself, as if dropped."""
        data = QGuiApplication.clipboard().mimeData()
        if data is None:
            return
        for url in data.urls() if data.hasUrls() else ():
            if url.isLocalFile():
                self.dropped.emit(url.toLocalFile())
                return
        if data.hasImage():
            image = QGuiApplication.clipboard().image()
            if not image.isNull():
                path = os.path.join(tempfile.mkdtemp(prefix="myeditor-cover-"),
                                    "pasted-cover.png")
                if image.save(path, "PNG"):
                    self.dropped.emit(path)

    @staticmethod
    def _picture_path(event) -> str:
        for url in event.mimeData().urls() if event.mimeData().hasUrls() else ():
            if url.isLocalFile():
                return url.toLocalFile()
        return ""

    def dragEnterEvent(self, event) -> None:  # noqa: N802, Qt's name
        if self.isEnabled() and self._picture_path(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802, Qt's name
        path = self._picture_path(event)
        if not self.isEnabled() or not path:
            event.ignore()
            return
        event.acceptProposedAction()
        self.dropped.emit(path)


class CoverImageField(QWidget):
    """The cover (see the module docstring). ``changed(address)`` as it
    changes, ``picture_changed`` when the well's picture comes or goes,
    and ``problem(reason)`` says why a picture could not become the cover.

    One field can show the covers of several documents in turn (Article
    Details, for the tab in front): ``set_owner`` says whose it shows. A
    picture dropped is uploaded for the document it was dropped for; done
    while another is shown, it comes as ``landed_for(owner, address)``,
    for that document, instead of becoming the cover shown."""

    changed = Signal(str)
    problem = Signal(str)
    picture_changed = Signal()
    uploading_changed = Signal(bool)
    landed_for = Signal(object, str)

    def __init__(self, *, media_store: Optional[MediaStore] = None,
                 media_visibility: Optional[MediaVisibility] = None,
                 copy_maker: Optional[PublicCopyMaker] = None,
                 private_library: Optional[PrivateLibrary] = None,
                 profile: Callable[[], Optional[Profile]] = lambda: None,
                 saver: Optional[MediaSaver] = None,
                 is_dark: bool = False, stacked: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._media_store = media_store
        # The window's Save As (MediaSaver): one started in the picker goes
        # on when the picker, or the sheet it opened from, closes. None:
        # the picker's own, which goes with it.
        self._saver = saver
        self._media_visibility = media_visibility or MediaVisibility()
        self._copy_maker = copy_maker
        self._private_library = private_library
        self._profile = profile
        self._is_dark = is_dark
        # Kept as written in its file (setReadOnly), its window working (setBusy).
        self._read_only = self._busy = False
        self._thumb_hash = ""
        self._picture = QPixmap()
        # The dropped picture being uploaded (its upload's name), if any,
        # and the document it was dropped for; the document shown now.
        self._uploading = ""
        self._upload_owner: object = None
        self._owner: object = None
        self._loader: Optional[ThumbnailLoader] = (
            ThumbnailLoader(parent=self) if media_store is not None else None)
        if self._loader is not None:
            self._loader.ready.connect(self._on_thumb_ready)
        if media_store is not None:
            media_store.upload_finished.connect(self._on_uploaded)
            media_store.upload_failed.connect(self._on_upload_failed)
            media_store.upload_progress.connect(self._on_upload_progress)

        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("article_advanced_field")
        self.url_edit.setPlaceholderText("https://example.com/cover.png")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setAccessibleName(_("Cover image"))
        self.url_edit.textChanged.connect(self._on_url_changed)

        self.preview = _Well(wide=stacked)
        self.preview.setObjectName("article_cover_preview")
        if not stacked:
            self.preview.setFixedSize(THUMB)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setAccessibleName(_("Cover image"))
        self.preview.dropped.connect(self.drop)
        if media_store is not None:
            self.preview.setToolTip(_("Drop a picture here to upload it as the cover."))

        self.pick_button: Optional[QPushButton] = None
        if media_store is not None:
            self.pick_button = QPushButton(_("Choose…"))
            self.pick_button.setAccessibleName(_("Choose Cover Image"))
            self.pick_button.setToolTip(_("Choose a picture from your Media Library, or "
                                          "upload one."))
            self.pick_button.setAutoDefault(False)
            self.pick_button.clicked.connect(self.choose)
        self.remove_button = QPushButton(_("Remove"))
        self.remove_button.setAccessibleName(_("Remove Cover Image"))
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self.url_edit.clear)
        self.adjust_button = QPushButton(_("Adjust…"))
        self.adjust_button.setAccessibleName(_("Adjust Cover Image"))
        self.adjust_button.setToolTip(_("Choose a cover format and move the frame to keep your subject in view."))
        self.adjust_button.setAutoDefault(False)
        self.adjust_button.clicked.connect(self.adjust)
        self.adjust_button.setVisible(media_store is not None)
        self.adjust_button.setEnabled(False)

        buttons = [button for button in (self.pick_button, self.adjust_button, self.remove_button) if button]
        self.address_line: Optional[ElidingLabel] = None
        if stacked:
            self._stack(buttons)
        else:
            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(4)
            row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            row.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
            column = QVBoxLayout()
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(6)
            for button in buttons:
                column.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
            column.addStretch(1)
            row.addLayout(column, 1)
            outer.addLayout(row)
            # The address is the window's to place; until then it is not shown.
            self.url_edit.hide()
        self._guidance = QLabel(_("Wide 16:9 is recommended. Aim for 1600 × 900 px."))
        self._guidance.setWordWrap(True)
        self._guidance.setObjectName("article_field_hint")
        self.layout().addWidget(self._guidance)
        self._dimensions = QLabel()
        self._dimensions.setWordWrap(True)
        self._dimensions.setObjectName("article_field_hint")
        self.layout().addWidget(self._dimensions)
        self._dimensions.hide()
        self.set_dark(is_dark)
        for previous, following in zip(buttons, buttons[1:]):
            QWidget.setTabOrder(previous, following)
        self._show_placeholder(_("No cover\nselected"))
        self.remove_button.setVisible(False)

    def text(self) -> str:
        return self.url_edit.text()

    def setText(self, text: str) -> None:  # noqa: N802, Qt's name
        self.url_edit.setText(text)

    def uploading(self) -> bool:
        """Whether a picture dropped on the well for the document shown is
        on its way up: the cover it will be is not there yet."""
        return bool(self._uploading) and self._upload_owner is self._owner

    def set_owner(self, owner: object, cover: Optional[str] = None) -> None:
        """The field shows ``owner``'s cover now, ``cover`` (None: the
        address as it is), without a word of change. A picture uploading
        for another document goes on for that one; one uploading for
        ``owner`` shows again."""
        if owner is self._owner:
            return
        was = self.uploading()
        self._owner = owner
        if cover is not None:
            self.url_edit.blockSignals(True)
            self.url_edit.setText(cover)
            self.url_edit.blockSignals(False)
        if self.uploading():
            self.remove_button.setVisible(bool(self.url_edit.text().strip()))
            self._show_placeholder(_("Uploading…"))
        else:
            self._on_url_changed(self.url_edit.text(), quiet=True)
        if self.uploading() != was:
            self.uploading_changed.emit(self.uploading())

    def picture(self) -> QPixmap:
        """The cover's picture, as large as it was loaded; null when none
        is shown."""
        return self._picture

    def setReadOnly(self, read_only: bool) -> None:  # noqa: N802, Qt's name
        """A cover that stays as it is (kept as written in its file): read
        only, and nothing said of picking one."""
        self._read_only = read_only
        self._guidance.setVisible(not read_only)
        self.adjust_button.setVisible(not read_only and self._media_store is not None)
        self._enable()

    def setBusy(self, busy: bool) -> None:  # noqa: N802, Qt's name
        """While the window it is in works (publishing): nothing can be
        typed, picked, dropped or removed, and it is shown as it was."""
        self._busy = busy
        self._enable()

    def _enable(self) -> None:
        fixed = self._read_only or self._busy
        self.url_edit.setReadOnly(fixed)
        self.preview.setEnabled(not fixed)
        if self.pick_button is not None:
            self.pick_button.setEnabled(not fixed)
        self.remove_button.setEnabled(not fixed)
        self.adjust_button.setEnabled(not fixed and not self.uploading() and not self._picture.isNull())

    def _stack(self, buttons) -> None:
        """The inspector's layout (see the module): the well across the
        column, Choose and Remove, the address as a line."""
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addWidget(self.preview)
        self.preview.cleared.connect(self._remove_if_any)
        self.preview.activated.connect(self.choose)
        self.preview.copied.connect(self._copy_address)
        self.preview.setAccessibleDescription(_("Drop or paste a picture, or press Return to "
                                                "choose one."))
        if self.pick_button is not None:
            # A pull-down: where the picture comes from.
            self.pick_button.setText(_("Choose"))
            self.pick_button.clicked.disconnect(self.choose)
            menu = QMenu(self.pick_button)
            menu.addAction(_("Media Library…"), self.choose)
            menu.addAction(_("Web Address…"), self.show_address_field)
            self.pick_button.setMenu(menu)
        # Choose, Adjust… and Remove wrap where the column is narrow.
        row = FlowLayout(spacing=6)
        for button in buttons:
            row.addWidget(button)
        column.addLayout(row)
        self.address_line = ElidingLabel()
        self.address_line.setObjectName("article_field_hint")
        self.address_line.setAccessibleName(_("Cover image"))
        column.addWidget(self.address_line)
        column.addWidget(self.url_edit)
        # Typed only when asked for, unless there is no library to pick from.
        self.url_edit.setVisible(self._media_store is None)
        self.url_edit.editingFinished.connect(self._address_typed)
        self.url_edit.textChanged.connect(self._show_address)
        self._show_address()

    def show_address_field(self) -> None:
        """Web Address…: the field, with the address to edit or type."""
        if self.address_line is None:
            return
        self.url_edit.show()
        self._show_address()
        self.url_edit.setFocus()
        self.url_edit.selectAll()

    def _address_typed(self) -> None:
        if self.address_line is not None and self._media_store is not None:
            self.url_edit.setVisible(False)
            self._show_address()

    def _show_address(self, *_args) -> None:
        if self.address_line is None:
            return
        address = self.url_edit.text().strip()
        self.address_line.set_full_text(address)
        self.address_line.setToolTip(address)
        self.address_line.setVisible(bool(address) and not self.url_edit.isVisible())

    def _remove_if_any(self) -> None:
        if self.url_edit.text() and self.remove_button.isEnabled():
            self.url_edit.clear()

    def _copy_address(self) -> None:
        if self.url_edit.text().strip():
            QGuiApplication.clipboard().setText(self.url_edit.text().strip())

    def set_dark(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        muted = appearance.roles(is_dark).muted
        for label in (self._guidance, self._dimensions):
            label.setStyleSheet(f"color: {muted};")

    # -- choosing a picture -------------------------------------------------

    def adjust(self) -> None:
        """Upload a crop as a new cover; cancellation leaves the document untouched."""
        if (self._read_only or self._busy or self.uploading() or self._media_store is None
                or self._picture.isNull()):
            return
        owner, address = self._owner, self.text()
        profile_key = getattr(self._profile(), "user_pubkey", None)
        dialog = CoverCropDialog(self._picture.toImage(), is_dark=self._is_dark,
                                 jpeg=(media_files.type_of(address) == "image/jpeg"), parent=self)
        try:
            accepted = dialog.exec()
            cover = dialog.cover
        finally:
            dialog.deleteLater()
        # A nested sheet still processes account/tab/cover changes.
        if (not accepted or cover is None or owner is not self._owner or address != self.text()
                or profile_key != getattr(self._profile(), "user_pubkey", None)
                or self._read_only or self._busy or self.uploading()):
            return
        self._start_upload(cover.data, cover.mime, "cover" + media_files.extension_for(cover.mime))

    def choose(self) -> None:
        """The Media Library's picker for a cover (choose_picture; a cover
        has no alternative text on Nostr)."""
        if self._media_store is None:
            return
        services = MediaServices(
            store=self._media_store, visibility=self._media_visibility,
            copy_maker=self._copy_maker, private_library=self._private_library,
            profile=self._profile, saver=self._saver)
        choose_picture(services, title=_("Choose Cover Image"), is_dark=self._is_dark,
                       parent=self, on_picked=self.picked)

    def picked(self, media: MediaFile, _alt: str = "") -> None:
        """``media`` chosen: its public address, or nothing and why."""
        resolved = resolve_pick(media, visibility=self._media_visibility,
                                maker=self._copy_maker, is_dark=self._is_dark, parent=self)
        if not resolved.ok:
            if resolved.reason:
                self.problem.emit(resolved.reason)
            return
        self.url_edit.setText(resolved.url)
        sha = resolved.sha256.lower()
        if sha != self._thumb_hash:
            self._thumb_hash = sha
            if self._loader is not None and (resolved.mime or "").startswith("image/"):
                self._loader.load(resolved.sha256, resolved.url)

    def drop(self, path: str) -> None:
        """A picture file dropped on the well: uploaded as a public
        picture, without what it reveals besides its pixels, and the cover
        once it is on the media servers."""
        if self._read_only or self._busy:
            return
        if self._media_store is None:
            self.problem.emit(_("A picture can be uploaded once you use a Nostr account with "
                                "media servers. Paste its web address instead."))
            return
        if not (media_files.type_of(path) or "").startswith("image/"):
            self.problem.emit(_("Only a picture can be a cover."))
            return
        try:
            with open(path, "rb") as file:
                data = file.read()
            public = scrub_for_publication(data, media_files.type_of(path) or "")
        except OSError as exc:
            self.problem.emit(_("Could not read file: {error}").format(
                error=exc.strerror or exc))
            return
        except ScrubError as exc:
            self.problem.emit(_("This picture can’t be a cover: {reason}.").format(reason=exc))
            return
        self._start_upload(public.data, public.mime, os.path.basename(path) or "cover")

    def _start_upload(self, data: bytes, mime: str, name: str) -> None:
        # The upload is known by a name of its own: another upload of a
        # file of the same name (from the Media Library) is not this one.
        stem, extension = os.path.splitext(name)
        self._uploading = f"{stem}-{uuid.uuid4().hex[:8]}{extension}"
        self._upload_owner = self._owner
        self._show_placeholder(_("Uploading…"))
        self.uploading_changed.emit(True)
        self._media_store.upload_bytes(data, name=self._uploading, mime_type=mime)

    def _on_upload_progress(self, name: str, sent: int, total: int) -> None:
        if self.uploading() and name == self._uploading and total > 0:
            percent = min(100, sent * 100 // total)
            self.preview.setText(_("Uploading…\n{percent} %").format(percent=percent))

    def _on_uploaded(self, name: str, media: MediaFile) -> None:
        if not self._uploading or name != self._uploading:
            return
        if not self.uploading():
            # Dropped for another document than the one shown: its cover.
            self._uploading = ""
            self.landed_for.emit(self._upload_owner, media.url)
            return
        self._uploading = ""
        # Uploaded here, from the picture's own pixels: public by making.
        self.url_edit.setText(media.url)
        if self._loader is not None:
            self._thumb_hash = media.hash.lower()
            self._loader.load(media.hash, media.url)
        self.uploading_changed.emit(False)

    def _on_upload_failed(self, name: str, reason: str) -> None:
        if not self._uploading or name != self._uploading:
            return
        if not self.uploading():
            self._uploading = ""             # another document's: its cover stays
            return
        self._uploading = ""
        self._on_url_changed(self.url_edit.text(), quiet=True)
        self.uploading_changed.emit(False)
        self.problem.emit(_("The picture was not uploaded: {reason}").format(reason=reason))

    # -- the well -------------------------------------------------------------

    def _on_url_changed(self, text: str, *, quiet: bool = False) -> None:
        if self.uploading() and not quiet:
            # A cover typed, pasted, picked or removed while a dropped
            # picture uploads: that one is the cover, not the upload.
            self._uploading = ""
            self.uploading_changed.emit(False)
        address = text.strip()
        sha = library_picture_hash(self._media_store, address) if self._loader else ""
        self.remove_button.setVisible(bool(address))
        if not address:
            self._show_placeholder(_("No cover\nselected"))
        elif sha and sha == self._thumb_hash and not self._picture.isNull():
            pass                                   # this picture is shown
        elif sha:
            # A picture of the library: its preview, from the library's copy.
            self._show_placeholder("")
            self._thumb_hash = sha
            self._loader.load(sha, address)
        else:
            # Typed, with no picture of ours behind it: no preview, said so.
            self._show_placeholder(_("Preview shown\nfor library picks"))
        if not quiet:
            self.changed.emit(text)

    def _show_placeholder(self, words: str) -> None:
        self._thumb_hash = ""
        had_picture = not self._picture.isNull()
        self._picture = QPixmap()
        self._dimensions.hide()
        self._enable()
        self.preview.setPixmap(QPixmap())
        self.preview.setText(words)
        if had_picture:
            self.picture_changed.emit()

    def _on_thumb_ready(self, sha: str, _path: str, pix: QPixmap) -> None:
        if sha.lower() != self._thumb_hash or pix.isNull():
            return
        self._picture = pix
        # Whole: the well crops it to its own frame, at any size.
        self.preview.setPixmap(pix)
        self.preview.setText("")
        self._dimensions.setText(_("Selected image: {width} × {height} px").format(
            width=pix.width(), height=pix.height()))
        self._dimensions.show()
        self._enable()
        self.picture_changed.emit()
