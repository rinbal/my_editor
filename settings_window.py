# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings: the few choices MyEditor keeps for the whole app.

Apple's Human Interface Guidelines give app-wide settings a window of their
own (Settings…, Command-comma on a Mac; Ctrl+comma elsewhere), so there is
one place to look for how MyEditor behaves instead of hunting through
menus. MyEditor's had no such place: the appearance and the language sat in
the View menu.

The window holds only what is chosen once and kept: the appearance, the
language, the writing font and the text size (which View > Zoom In and
Zoom Out step through too), whether the format bar shows over a selection,
how the text cursor looks, and, while a Nostr account is in use, when a
picture, audio or video added to a document goes to the media servers and
where published posts open on the web.
Every command stays in its menu: the window
shows the menu commands' state and chooses through them (or through what
they call), so the two never disagree, and it holds no feature logic of its
own.

As in a Mac settings window, a choice applies at once: there is no OK,
Apply or Cancel. Escape and Command-W (Ctrl+W) close it. It is not modal.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFontDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

import alerts
import dictation as dictation_mod
import fonts
import theme
from dictation import models as dictation_models
from dictation.preferences import AUTOMATIC, LANGUAGES, Preferences
from dictation.setup import size_text
from constants import DARK_MUTED_FG, LIGHT_MUTED_FG
from i18n import _
from nostr import web_readers
from text_size import text_size

# The pop-up's item that asks for another installed font.
_OTHER_FONT = "other"

# When a picture, audio or video added to a document goes to the media
# servers while a Nostr account is in use, as the setting stores it, in the
# order offered: once it is published (or saved as a draft), or at once.
UPLOAD_CHOICES = ("publishing", "now")


# The text cursors offered, as the setting stores them (editor.CARET_STYLES).
CARET_CHOICES = ("thin", "medium", "thick", "block")

# How wide the explanations under the choices are.
_HELP_WIDTH = 320


def _caret_titles() -> Dict[str, str]:
    return {"thin": _("Thin"), "medium": _("Medium"), "thick": _("Thick"),
            "block": _("Block")}


def _upload_titles() -> Dict[str, str]:
    return {"publishing": _("When Publishing"), "now": _("Right Away")}


class SettingsWindow(QDialog):
    """App-wide settings, applied as they are chosen (see the module docstring).

    ``appearance`` maps "system", "light" and "dark" to View > Appearance's
    commands; ``languages`` lists View > Language's as ``(code, command)``;
    ``choose_language(code)`` remembers one; ``writing_family()`` and
    ``choose_writing_family(family)`` read and store the writing font
    (fonts.SYSTEM, SERIF, MONOSPACE or a family's name; View > Use
    Monospace Font for Writing is MONOSPACE); the text size is the app's
    (text_size.py), which View > Zoom In and Zoom Out change too;
    ``format_bar()`` and
    ``choose_format_bar(on)`` read and store whether the format bar shows
    over a selection; ``caret_style()`` and ``choose_caret_style(value)``
    read and store the text cursor (CARET_CHOICES); ``upload_choice()`` and
    ``choose_upload(value)`` read and store when what is added to a
    document is uploaded (UPLOAD_CHOICES), and
    ``web_reader()`` and ``choose_web_reader(key)`` the reading app
    published posts open in (web_readers), both offered while
    ``nostr_in_use()``.
    """

    def __init__(self, *, appearance: Dict[str, QAction],
                 languages: List[Tuple[str, QAction]],
                 choose_language: Callable[[str], None],
                 writing_family: Callable[[], str],
                 choose_writing_family: Callable[[str], None],
                 upload_choice: Callable[[], str],
                 choose_upload: Callable[[str], None],
                 nostr_in_use: Callable[[], bool],
                 format_bar: Callable[[], bool],
                 choose_format_bar: Callable[[bool], None],
                 caret_style: Callable[[], str] = lambda: "medium",
                 choose_caret_style: Callable[[str], None] = lambda value: None,
                 web_reader: Callable[[], str] = lambda: web_readers.DISCOVER,
                 choose_web_reader: Callable[[str], None] = lambda key: None,
                 dictation_ways: Tuple[str, ...] = (),
                 dictation: Callable[[], Preferences] = Preferences,
                 choose_dictation: Callable[[Preferences], None] = lambda prefs: None,
                 is_dark: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(_("Settings"))
        self._appearance = appearance
        self._languages = languages
        self._choose_language = choose_language
        self._upload_choice = upload_choice
        self._choose_upload = choose_upload
        self._nostr_in_use = nostr_in_use
        self._format_bar_shown = format_bar
        self._web_reader = web_reader
        self._writing_family = writing_family
        self._choose_writing_family = choose_writing_family

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(14)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)

        # Appearance: the menu's three commands, as radio buttons.
        self.appearance_buttons: Dict[str, QRadioButton] = {}
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        for mode, action in appearance.items():
            button = QRadioButton(action.text())
            button.setChecked(action.isChecked())
            button.toggled.connect(lambda on, a=action: on and not a.isChecked() and a.trigger())
            action.toggled.connect(self._show_appearance)
            self.appearance_buttons[mode] = button
            column.addWidget(button)
        column.addWidget(self._help(_("Automatic follows your computer’s light or dark "
                                      "appearance as it changes.")))
        form.addRow(self._label(_("Appearance:")), box)

        # Language: View > Language's choices, as a pop-up menu.
        self.language = QComboBox()
        for code, action in languages:
            self.language.addItem(action.text(), code)
            action.toggled.connect(self._show_chosen_language)
        self._show_chosen_language()
        self.language.activated.connect(self._language_activated)
        language_box = QWidget()
        language_column = QVBoxLayout(language_box)
        language_column.setContentsMargins(0, 0, 0, 0)
        language_column.setSpacing(4)
        language_column.addWidget(self.language, 0, Qt.AlignLeft)
        language_column.addWidget(self._help(
            _("MyEditor uses a new language the next time it opens.")))
        form.addRow(self._label(_("Language:"), self.language), language_box)

        # The writing font and its size, side by side, as a Mac's settings
        # name a font ("Helvetica 12"): each font named in itself, Other…
        # the system's font panel; the sizes in points. View > Zoom In and
        # Zoom Out step through the sizes too.
        self.font = QComboBox()
        self.font.activated.connect(self._font_activated)
        self.font_size = QComboBox()
        self.font_size.setAccessibleName(_("Font size"))
        self.font_size.view().setAccessibleName(_("Font size"))
        self.font_size.activated.connect(
            lambda index: text_size().set_points(self.font_size.itemData(index)))
        text_size().changed.connect(self._show_font_size)
        font_row = QHBoxLayout()
        font_row.setContentsMargins(0, 0, 0, 0)
        font_row.setSpacing(8)
        font_row.addWidget(self.font)
        font_row.addWidget(self.font_size)
        font_row.addStretch(1)
        font_box = QWidget()
        font_column = QVBoxLayout(font_box)
        font_column.setContentsMargins(0, 0, 0, 0)
        font_column.setSpacing(4)
        font_column.addLayout(font_row)
        font_column.addWidget(self._help(
            _("The font you write in and its size. Code and plain text stay in the "
              "monospace font.")))
        form.addRow(self._label(_("Font:"), self.font), font_box)

        # Writing: the format bar over text selected with the pointer.
        self.format_bar = QCheckBox(_("Show the format bar when selecting text"))
        self.format_bar.setChecked(bool(format_bar()))
        self.format_bar.toggled.connect(choose_format_bar)
        form.addRow(self._label(_("Writing:")), self.format_bar)

        # The text cursor: how the insertion point looks where you type.
        self.caret = QComboBox()
        caret_titles = _caret_titles()
        for value in CARET_CHOICES:
            self.caret.addItem(caret_titles[value], value)
        self.caret.setCurrentIndex(max(0, self.caret.findData(caret_style())))
        self.caret.activated.connect(
            lambda index: choose_caret_style(self.caret.itemData(index)))
        caret_box = QWidget()
        caret_column = QVBoxLayout(caret_box)
        caret_column.setContentsMargins(0, 0, 0, 0)
        caret_column.setSpacing(4)
        caret_column.addWidget(self.caret, 0, Qt.AlignLeft)
        caret_column.addWidget(self._help(
            _("How the cursor looks where you type, in every document.")))
        form.addRow(self._label(_("Text cursor:"), self.caret), caret_box)

        # When added media is uploaded: only while Nostr is in use (nothing
        # of Nostr before).
        self.upload = QComboBox()
        titles = _upload_titles()
        for value in UPLOAD_CHOICES:
            self.upload.addItem(titles[value], value)
        self.upload.activated.connect(
            lambda index: self._choose_upload(self.upload.itemData(index)))
        self._upload_box = QWidget()
        upload_column = QVBoxLayout(self._upload_box)
        upload_column.setContentsMargins(0, 0, 0, 0)
        upload_column.setSpacing(4)
        upload_column.addWidget(self.upload, 0, Qt.AlignLeft)
        upload_column.addWidget(self._help(
            _("Pictures, audio and video you add stay on this computer until you publish. "
              "Right Away puts them on your media servers as soon as you add them.")))
        self._upload_label = self._label(_("Upload files:"), self.upload, nostr=True)
        form.addRow(self._upload_label, self._upload_box)

        # Where published posts open on the web: only while Nostr is in use.
        self.reader = QComboBox()
        for app in web_readers.READERS:
            self.reader.addItem(app.name, app.key)
        self.reader.activated.connect(
            lambda index: choose_web_reader(self.reader.itemData(index)))
        self._reader_box = QWidget()
        reader_column = QVBoxLayout(self._reader_box)
        reader_column.setContentsMargins(0, 0, 0, 0)
        reader_column.setSpacing(4)
        reader_column.addWidget(self.reader, 0, Qt.AlignLeft)
        reader_column.addWidget(self._help(
            _("Where your published posts open on the web, and the app a copied link "
              "points to. A post the app cannot show, such as a short note in Standup "
              "Discover, opens in njump.")))
        self._reader_label = self._label(_("Open posts on the web in:"), self.reader, nostr=True)
        form.addRow(self._reader_label, self._reader_box)

        self._dictation = dictation
        self._choose_dictation = choose_dictation
        self.dictation_way = self.dictation_model = self.dictation_language = None
        if dictation_mod.ON_DEVICE in dictation_ways:
            self._add_dictation_rows(form, dictation_ways)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.addLayout(form)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetFixedSize)

        # Command-W on a Mac, Ctrl+W (and Ctrl+F4 on Windows) elsewhere, as
        # for any window: every key the system binds to Close, not only its
        # first (a QKeySequence of a standard key holds one).
        close = QShortcut(QKeySequence.StandardKey.Close, self)
        close.activated.connect(self.close)
        self.apply_theme(is_dark)
        self.refresh()

    def _fit_help(self) -> None:
        """Give every explanation the height of its words, in the font the
        stylesheet gives it."""
        for label in self.findChildren(QLabel, "help"):
            label.ensurePolished()
            label.setFixedHeight(label.heightForWidth(_HELP_WIDTH))

    # -- building -------------------------------------------------------------

    @staticmethod
    def _label(text: str, buddy: Optional[QComboBox] = None, *, nostr: bool = False) -> QLabel:
        """A field's label; a pop-up menu (and the list it opens) is named by it."""
        from nostr.ui.context_widgets import NostrLabel
        label = NostrLabel(text) if nostr else QLabel(text)
        if buddy is not None:
            label.setBuddy(buddy)
            name = text.rstrip(":").strip()
            buddy.setAccessibleName(name)
            buddy.view().setAccessibleName(name)
        return label

    @staticmethod
    def _help(text: str) -> QLabel:
        """A line under a choice that says what it does. A fixed width, and
        the height its words take at that width (in any language), so the
        window lays it out whole instead of guessing."""
        label = QLabel(text)
        label.setObjectName("help")
        label.setWordWrap(True)
        label.setFixedWidth(_HELP_WIDTH)
        return label

    # -- state ------------------------------------------------------------------

    def _show_appearance(self, *_args) -> None:
        for mode, action in self._appearance.items():
            if action.isChecked():
                self.appearance_buttons[mode].setChecked(True)

    def _show_chosen_language(self, *_args) -> None:
        for index, (_code, action) in enumerate(self._languages):
            if action.isChecked():
                self.language.setCurrentIndex(index)
                return

    def _language_activated(self, index: int) -> None:
        code = self.language.itemData(index)
        if code is not None:
            self._choose_language(code)

    # -- the writing font and the text size ------------------------------------

    def _show_fonts(self) -> None:
        """The fonts offered, the chosen one current: System, Serif and
        Monospace (each named in itself), a font picked with Other… by its
        name, then Other…."""
        chosen = self._writing_family() or fonts.SYSTEM
        self.font.clear()
        offered = [(fonts.SYSTEM, _("System")), (fonts.SERIF, _("Serif")),
                   (fonts.MONOSPACE, _("Monospace"))]
        if chosen not in (fonts.SYSTEM, fonts.SERIF, fonts.MONOSPACE):
            offered.append((chosen, chosen))
        for family, title in offered:
            self.font.addItem(title, family)
            sample = fonts.writing_font(13, family)
            self.font.setItemData(self.font.count() - 1, sample, Qt.ItemDataRole.FontRole)
        self.font.insertSeparator(self.font.count())
        self.font.addItem(_("Other…"), _OTHER_FONT)
        self.font.setCurrentIndex(max(0, self.font.findData(chosen)))

    def _font_activated(self, index: int) -> None:
        family = self.font.itemData(index)
        if family == _OTHER_FONT:
            current = fonts.writing_font(fonts.WRITING_PIXEL_SIZE, self._writing_family())
            current.setPointSize(text_size().points())
            picked, accepted = QFontDialog.getFont(current, self, _("Writing Font"))
            if accepted:
                # The panel sets a size with the font, as on a Mac.
                self._choose_writing_family(picked.family())
                if picked.pointSizeF() > 0:
                    text_size().set_points(picked.pointSizeF())
            self._show_fonts()                    # Other… is never the choice
            return
        if family:
            self._choose_writing_family(family)

    def _show_font_size(self, *_args) -> None:
        """The sizes offered, in points, the current one chosen."""
        size = text_size()
        self.font_size.clear()
        for points in size.sizes():
            self.font_size.addItem(_("{size} pt").format(size=points), points)
        self.font_size.setCurrentIndex(self.font_size.findData(size.points()))

    # -- dictation -----------------------------------------------------------------

    def _add_dictation_rows(self, form: QFormLayout, ways: Tuple[str, ...]) -> None:
        """Dictation, where it can run on this computer (Windows, Linux): the
        way, where there is a choice; the speech model, and removing it;
        the language dictated."""
        if len(ways) > 1:
            self.dictation_way = QComboBox()
            titles = {dictation_mod.SYSTEM: _("Windows Voice Typing"),
                      dictation_mod.ON_DEVICE: _("On This Computer")}
            for way in ways:
                self.dictation_way.addItem(titles[way], way)
            self.dictation_way.activated.connect(
                lambda index: self._dictation_chosen(way=self.dictation_way.itemData(index)))
            way_box = self._column(self.dictation_way, _(
                "Windows Voice Typing needs the internet. On this computer, dictation works "
                "offline after a one-time download, and your voice never leaves it."))
            form.addRow(self._label(_("Dictation:"), self.dictation_way), way_box)

        self.dictation_model = QComboBox()
        for key, title in ((dictation_models.STANDARD, _("Standard ({size})")),
                           (dictation_models.ACCURATE, _("More accurate ({size})"))):
            model = dictation_models.MODELS[key]
            size = model.size + dictation_models.VOICE_DETECTOR.size
            self.dictation_model.addItem(title.format(size=size_text(size)), key)
        self.dictation_model.activated.connect(
            lambda index: self._dictation_chosen(model=self.dictation_model.itemData(index)))
        self.dictation_status = self._help("")
        self.dictation_remove = QPushButton(_("Remove…"))
        self.dictation_remove.setAccessibleName(_("Remove the speech model"))
        self.dictation_remove.clicked.connect(self._remove_dictation_model)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(8)
        model_row.addWidget(self.dictation_model)
        model_row.addWidget(self.dictation_remove)
        model_row.addStretch(1)
        model_box = QWidget()
        model_column = QVBoxLayout(model_box)
        model_column.setContentsMargins(0, 0, 0, 0)
        model_column.setSpacing(4)
        model_column.addLayout(model_row)
        model_column.addWidget(self.dictation_status)
        form.addRow(self._label(_("Speech model:"), self.dictation_model), model_box)

        self.dictation_language = QComboBox()
        self.dictation_language.addItem(_("Automatic"), AUTOMATIC)
        self.dictation_language.insertSeparator(1)
        for code, name in LANGUAGES:
            self.dictation_language.addItem(name, code)
        self.dictation_language.activated.connect(lambda index: self._dictation_chosen(
            language=self.dictation_language.itemData(index)))
        language_box = self._column(self.dictation_language, _(
            "The language you dictate in. Automatic hears it, but naming it writes "
            "better."))
        form.addRow(self._label(_("Dictation language:"), self.dictation_language),
                    language_box)
        self._show_dictation()

    def _column(self, control: QWidget, help_text: str) -> QWidget:
        box = QWidget()
        column = QVBoxLayout(box)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(control, 0, Qt.AlignLeft)
        column.addWidget(self._help(help_text))
        return box

    def _dictation_chosen(self, **changes) -> None:
        self._choose_dictation(self._dictation().with_(**changes))
        self._show_dictation()

    def _show_dictation(self) -> None:
        if self.dictation_model is None:
            return
        prefs = self._dictation()
        way = prefs.way
        if self.dictation_way is not None:
            index = self.dictation_way.findData(way)
            self.dictation_way.setCurrentIndex(max(0, index))
            way = self.dictation_way.currentData()
        on_device = way == dictation_mod.ON_DEVICE or self.dictation_way is None
        self.dictation_model.setCurrentIndex(max(0, self.dictation_model.findData(prefs.model)))
        self.dictation_language.setCurrentIndex(
            max(0, self.dictation_language.findData(prefs.language)))
        for control in (self.dictation_model, self.dictation_language):
            control.setEnabled(on_device)
        installed = dictation_models.installed(dictation_models.MODELS[prefs.model])
        self.dictation_status.setText(_("Downloaded.") if installed
                                      else _("Downloaded the first time you dictate."))
        self.dictation_remove.setVisible(installed)
        self.dictation_remove.setEnabled(on_device)

    def _remove_dictation_model(self) -> None:
        model = dictation_models.MODELS[self._dictation().model]
        if alerts.confirm_destructive(
                self, title=_("Remove the speech model?"),
                message=_("It is downloaded again the next time you dictate on this computer."),
                action=_("Remove")):
            dictation_models.remove(model)
        self._show_dictation()

    def refresh(self) -> None:
        """Read again what can change elsewhere: whether Nostr is in use, the
        writing font and the text size (the View menu changes them too), and
        the format bar's setting."""
        self._show_fonts()
        self._show_font_size()
        self.format_bar.setChecked(bool(self._format_bar_shown()))
        shown = bool(self._nostr_in_use())
        self._upload_label.setVisible(shown)
        self._upload_box.setVisible(shown)
        self._reader_label.setVisible(shown)
        self._reader_box.setVisible(shown)
        chosen = self.reader.findData(self._web_reader())
        self.reader.setCurrentIndex(chosen if chosen >= 0 else 0)
        index = self.upload.findData(self._upload_choice())
        self.upload.setCurrentIndex(index if index >= 0 else 0)

    def showEvent(self, event) -> None:  # noqa: N802, Qt's name
        self.refresh()
        super().showEvent(event)
        from nostr.ui.native_appearance import apply_chrome
        apply_chrome(self, self._is_dark, nostr=False)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        muted = DARK_MUTED_FG if is_dark else LIGHT_MUTED_FG
        self.setStyleSheet(theme.dialog_stylesheet(is_dark)
                           + f"QLabel#help {{ color: {muted}; font-size: 11px; }}")
        self._fit_help()
        if self.isVisible():
            from nostr.ui.native_appearance import apply_chrome
            apply_chrome(self, is_dark, nostr=False)
