# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source Settings: one source's name, its checks and its defaults.

A sheet on the Imports window, from a source's context menu (and the
list's "…" menu), as EINUNDZWANZIG STANDUP's SourceSettingsSheet:

    [icon]  A thoughtful journal
            journal.example                      opens the website

        Name:  [A thoughtful journal        ]
     Address:  https://journal.example/feed
     Updates:  Checked 4 min ago            [Check Now]
               [x] Check for new posts automatically
  New drafts:  [x] Copy images to your media server
               [x] Fetch the full article
               Your other devices use these settings too.

    [Unsubscribe…]                         [Cancel]  [Done]

Done keeps what changed, Cancel nothing: the name and the defaults for
new drafts travel with the list of sources (one change to send, not one
per click), whether the source is checked automatically stays on this
computer. Check Now and Unsubscribe act at once; Unsubscribe asks first
(alerts.confirm_destructive, Cancel the default). A source that is not
a feed is read when it is opened (the owner's decision Q-2): it shows
that line instead of the checks.

The sheet asks the controller (settings_of, save_settings, check_now,
unfollow) and keeps no state of its own beyond what is on screen.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import alerts
import url_safety
from i18n import _

from ..imports.inbox_store import MANUAL_CHECK_GAP
from ..imports.workspace import freshness
from .assistant import DEFAULT, LEADING, NORMAL, link_button, text_label
from .eliding_label import ElidingLabel
from .imports_glyphs import source_avatar
from .imports_sheets import _Sheet

ICON = 32
NAME_LENGTH = 200


def unsubscribe_question(title: str) -> dict:
    """What Unsubscribe asks, from the sheet or a context menu."""
    return dict(title=_("Unsubscribe from “{title}”?").format(title=title),
                message=_("Its posts leave Imports. Drafts you already created stay."),
                action=_("Unsubscribe"))


class SourceSettingsSheet(_Sheet):
    """Source Settings for one source (see the module docstring).

    Signals:
      unsubscribed(str)   the source was unfollowed (its key)
    """

    unsubscribed = Signal(str)

    def __init__(self, controller, key: str, *, dark: bool = False,
                 open_url: Optional[Callable[[QUrl], object]] = None,
                 confirm: Optional[Callable[..., bool]] = None, parent=None) -> None:
        super().__init__(controller, _("Source Settings"), dark=dark, parent=parent)
        self._key = key
        self._open_url = open_url
        self._confirm_with = confirm
        # "You can check again in a minute." for that minute at most.
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(MANUAL_CHECK_GAP * 1000)
        self._throttle.timeout.connect(self.refresh)
        settings = controller.settings_of(key)
        self._settings = settings
        self.heading.hide()
        self.column.addWidget(self._header(settings))
        self.column.addSpacing(4)
        self.column.addLayout(self._form(settings))
        self._add_busy()
        self._add_error()
        self._finish()
        self.refresh()
        # A check that ends while the sheet is open shows at once, and so
        # does the site's icon when it arrives (CP3 review L7).
        controller.sources_changed.connect(self.refresh)
        controller.icon_ready.connect(self._on_icon_ready)
        # The field the sheet is for, as the Follow and Link sheets do.
        self.name.setFocus()

    # -- parts ---------------------------------------------------------------------

    def _header(self, settings) -> QWidget:
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(ICON, ICON)
        title = settings.name or settings.own_title or settings.url
        self._show_icon()
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = ElidingLabel(title)
        self.title.setObjectName("title")
        text.addWidget(self.title)
        site = settings.website
        self.website = link_button(url_safety.host_of(site) or site, self._open_website)
        self.website.setToolTip(site)
        self.website.setAccessibleName(_("Open Website"))
        self.website.setAccessibleDescription(site)
        # A Nostr author or another source without a website shows none.
        self.website.setVisible(url_safety.is_safe_external_url(site))
        text.addWidget(self.website, 0, Qt.AlignmentFlag.AlignLeft)
        row.addLayout(text, 1)
        return header

    def _form(self, settings) -> QFormLayout:
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)

        self.name = QLineEdit(settings.name)
        self.name.setMaxLength(NAME_LENGTH)
        self.name.setPlaceholderText(settings.own_title or settings.url)
        self.name.textChanged.connect(self._on_name_edited)
        form.addRow(_("Name:"), self.name)

        self.address = QLabel(settings.url)
        self.address.setObjectName("muted")
        self.address.setTextFormat(Qt.TextFormat.PlainText)
        self.address.setWordWrap(True)
        self.address.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                             | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        form.addRow(_("Address:"), self.address)

        updates = QWidget()
        column = QVBoxLayout(updates)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        self.status = text_label("", "muted")
        status_row.addWidget(self.status, 1)
        self.check_now_button = QPushButton(_("Check Now"))
        alerts.answer_return_as_the_system_does([self.check_now_button])
        self.check_now_button.clicked.connect(self.check_now)
        status_row.addWidget(self.check_now_button, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(status_row)
        self.automatic = QCheckBox(_("Check for new posts automatically"))
        self.automatic.setChecked(settings.checking)
        column.addWidget(self.automatic)
        self.check_now_button.setVisible(settings.automatic)
        self.automatic.setVisible(settings.automatic)
        form.addRow(_("Updates:"), updates)

        drafts = QWidget()
        column = QVBoxLayout(drafts)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        self.copy_images = QCheckBox(_("Copy images to your media server"))
        self.copy_images.setChecked(settings.copy_images)
        column.addWidget(self.copy_images)
        column.addWidget(_hint(_("Drafts keep working if the original site removes its "
                                 "images.")))
        column.addSpacing(6)
        self.full_text = QCheckBox(_("Fetch the full article"))
        self.full_text.setChecked(settings.full_text)
        column.addWidget(self.full_text)
        column.addWidget(_hint(_("Uses the whole article when the feed only has a "
                                 "summary.")))
        column.addSpacing(6)
        column.addWidget(_hint(_("Your other devices use these settings too.")))
        form.addRow(_("New drafts:"), drafts)
        return form

    def _buttons(self) -> list:
        return [("unsubscribe", _("Unsubscribe…"), LEADING, self.unsubscribe, True),
                ("cancel", _("Cancel"), NORMAL, self.reject, True),
                ("done", _("Done"), DEFAULT, self.accept, True)]

    def _show_icon(self) -> None:
        """The site's icon, or the letter of its name until it is known."""
        settings = self._settings
        self.icon.setPixmap(source_avatar(
            settings.name or settings.own_title or settings.url, settings.url, ICON,
            dark=self._is_dark, dpr=self.devicePixelRatioF(),
            image=self._controller.site_icon(self._key, settings.url)))

    def _on_icon_ready(self, _site: str) -> None:
        self._show_icon()

    # -- state -----------------------------------------------------------------------

    def _display_name(self) -> str:
        settings = self._settings
        return self.name.text().strip() or settings.own_title or settings.url

    def _on_name_edited(self) -> None:
        self.title.setText(self._display_name())

    def refresh(self) -> None:
        """The status line: when the source was checked, a check under way,
        or why the last one failed and when the next try is."""
        controller = self._controller
        source = controller.source(self._key)
        if source is None:
            # Unfollowed (here, or on another device): nothing left to set.
            if self.isVisible():
                self.done(self.DialogCode.Rejected)
            return
        checking = controller.is_checking(self._key)
        if checking:
            text = _("Checking for new posts…")
        elif self._throttle.isActive():
            text = _("You can check again in a minute.")
        else:
            text = freshness(source)
        self.status.setText(text)
        self.status.setAccessibleName(text)
        self.check_now_button.setEnabled(not checking and not controller.read_only)

    def check_now(self) -> None:
        under_way = self._controller.is_checking(self._key)
        started = self._controller.check_now(self._key)
        if started or under_way:
            self._throttle.stop()
        else:
            self._throttle.start()
        self.refresh()

    def _open_website(self) -> None:
        site = self._settings.website
        if self._open_url is not None and url_safety.is_safe_external_url(site):
            self._open_url(QUrl(site))

    def _confirm(self, **question) -> bool:
        if self._confirm_with is not None:
            return bool(self._confirm_with(**question))
        return alerts.confirm_destructive(self, is_dark=self._is_dark, **question, nostr=True)

    def unsubscribe(self) -> None:
        """Ask, then stop following the source; the sheet closes."""
        if not self._confirm(**unsubscribe_question(self._display_name())):
            return
        if self._controller.unfollow(self._key):
            self.unsubscribed.emit(self._key)
        if self.isVisible():    # unfollowing closed it already, as a refresh
            self.done(self.DialogCode.Rejected)

    def accept(self) -> None:
        self._controller.save_settings(
            self._key, name=self.name.text(), checking=self.automatic.isChecked(),
            copy_images=self.copy_images.isChecked(), full_text=self.full_text.isChecked())
        super().accept()


def _hint(text: str) -> QLabel:
    label = text_label(text, "help")
    label.setIndent(24)     # under its check box's text, not its box
    return label
