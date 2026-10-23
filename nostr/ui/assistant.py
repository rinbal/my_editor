# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared shape of MyEditor's step-by-step windows.

Joining EINUNDZWANZIG, creating an account, restoring one: each is a short
sequence of pages in one window, and they should look and behave alike.
This module is that common part, so each window only says what its pages
contain and what happens between them.

What every assistant gets, following Apple's Human Interface Guidelines:

- One default button, at the trailing edge, where Return lands; buttons
  that back out (Cancel, Not Now) beside it; Go Back at the leading edge.
  A page lists its buttons as ``(key, label, placement, handler)`` and the
  window lays them out, so no page can put them anywhere else. A menu in
  place of the handler makes a pull-down button (Manage), whose click
  opens the menu's commands.
- Text that comes from outside (names, keys, server words) is plain text,
  never markup.
- Progress is shown where the work happens: an indeterminate bar while
  waiting, or a step list that marks each step pending, active, done or
  failed in words as well as colour.
- Colours come from the app's theme, light and dark.

``copy_secret`` puts a secret on the clipboard and takes it off again a
minute later, or when MyEditor quits, but only if it is still there:
nothing the person copied afterwards is ever cleared. It is marked as a
secret on the way in, so clipboard managers and clipboard history skip it:
org.nspasteboard.ConcealedType on a Mac (written to the pasteboard
directly, because Qt renames custom types there), the KDE password-manager
hint, and Windows' ExcludeClipboardContentFromMonitorProcessing.
"""

from __future__ import annotations

import atexit
import sys
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

import shiboken6
from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
    QVBoxLayout,
    QWidget,
)

from . import appearance
from alerts import answer_return_as_the_system_does
from format_toolbar import Disclosure, PopupButton
from i18n import _
from popup_placement import available_area

# Button placement.
LEADING = "leading"     # Go Back: the leading edge
NORMAL = "normal"       # Not Now, Close, Cancel: beside the default
DEFAULT = "default"     # the likely action: the trailing edge, answers Return

# What a button does: a handler, or a menu for a pull-down button.
ButtonSpec = Tuple[str, str, str, Union[Callable[[], None], QMenu]]

# A pull-down button's Disclosure arrow: where it ends, from the button's
# trailing edge, in the room the button's padding keeps for it.
PULL_DOWN_ARROW_END = 12

# The transparent edge around a link's words, where its focus ring shows.
LINK_EDGE = 2

SECRET_CLIPBOARD_SECONDS = 60


def stylesheet(is_dark: bool) -> str:
    """The app's dialog look plus what assistants use."""
    r = appearance.roles(is_dark)
    fg, muted, border, field = r.text, r.muted, r.edge, r.field
    accent = r.selection
    ok = r.success
    err = r.error
    link, ring = r.ink, r.focus
    # A pull-down keeps room for its arrow after its title: the arrow, and
    # as much space again before it (a pixel less with the focus ring, so
    # it keeps its size). It is as wide as that, not the width a short
    # button title is given. On Windows and Linux the default moves to the
    # button with the focus; a pull-down then keeps its plain fill, as a
    # destructive button does, since its arrow is drawn in the quiet ink.
    arrow_room = PULL_DOWN_ARROW_END + 2 * Disclosure.WIDTH
    return appearance.dialog_stylesheet(is_dark) + f"""
    QLabel#title {{ font-size: 16px; font-weight: 600; }}
    QLabel#muted, QLabel#help {{ color: {muted}; }}
    QLabel#help {{ font-size: 11px; }}
    QLabel#error {{ color: {err}; font-size: 11px; }}
    QLabel#item_title {{ font-weight: 600; }}
    QLabel#amount {{ font-size: 22px; font-weight: 600; }}
    QLabel#check {{ color: {ok}; font-size: 15px; font-weight: 700; }}
    QLabel#mono {{ font-family: "Menlo", "Consolas", monospace; }}
    QLabel#qr {{ background: #FFFFFF; border: 1px solid {border}; border-radius: 8px; padding: 6px; }}
    QLabel#step_badge {{
        border: 1px solid {border}; border-radius: 11px;
        color: {muted}; font-size: 11px; font-weight: 600;
    }}
    QLabel#step_badge[state="active"] {{ background: {accent}; border-color: {accent}; color: #FFFFFF; }}
    QLabel#step_badge[state="done"] {{ border-color: {ok}; color: {ok}; }}
    QLabel#step_badge[state="error"] {{ border-color: {err}; color: {err}; }}
    QLineEdit, QPlainTextEdit {{
        background: {field}; border: 1px solid {border}; border-radius: 5px; padding: 4px 6px;
    }}
    QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {accent}; }}
    QListWidget {{
        background: {field}; border: 1px solid {border}; border-radius: 5px; padding: 2px;
    }}
    QListWidget:focus {{ border-color: {accent}; }}
    QListWidget::item {{ padding: 3px 4px; }}
    QListWidget::item:disabled {{ color: {muted}; }}
    QPushButton:disabled {{ color: {muted}; }}
    QPushButton:default:disabled {{ background: {field}; color: {muted}; border-color: {border}; }}
    QPushButton#pull_down {{ padding-right: {arrow_room}px; min-width: 0; }}
    QPushButton#pull_down:focus {{ padding-right: {arrow_room - 1}px; }}
    QPushButton#pull_down:default {{
        background: {field}; color: {fg}; border: 2px solid {ring};
        padding: 5px {arrow_room - 1}px 5px 13px;
    }}
    QPushButton#link {{
        border: {LINK_EDGE}px solid transparent; border-radius: 3px; background: transparent;
        color: {link}; padding: 0px; min-width: 0;
    }}
    QPushButton#link:hover {{ text-decoration: underline; }}
    QPushButton#link:focus {{ border-color: {appearance.roles(is_dark).focus}; }}
    QPushButton#link[tone="destructive"] {{ color: {r.error}; }}
    QPushButton#link:disabled {{ color: {muted}; }}
    QRadioButton {{ font-weight: 600; }}
    QProgressBar {{
        background: {field}; border: 1px solid {border}; border-radius: 3px; max-height: 6px;
    }}
    QProgressBar::chunk {{ background: {accent}; border-radius: 2px; }}
    """


def text_label(text: str = "", name: str = "", *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.PlainText)
    return label


def link_button(text: str, handler: Callable[[], None]) -> QPushButton:
    """A text-only button for a secondary action that should not compete
    with the page's buttons."""
    button = QPushButton(text)
    button.setObjectName("link")
    # A link is not one of the window's buttons: it never takes Return
    # from the window's action, on any system (Space still presses it).
    button.setAutoDefault(False)
    button.setCursor(Qt.PointingHandCursor)
    button.clicked.connect(handler)
    return button


def busy_bar(width: int = 220) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 0)      # indeterminate: the wait has no known length
    bar.setTextVisible(False)
    bar.setMaximumWidth(width)
    bar.setAccessibleName(_("Working"))
    return bar


def page(spacing: int = 10) -> Tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    column = QVBoxLayout(widget)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(spacing)
    return widget, column


# Clipboard formats that say "this is a secret, don't keep it".
CONCEALED_MAC = "org.nspasteboard.ConcealedType"
CONCEALED_KDE = "x-kde-passwordManagerHint"
CONCEALED_WINDOWS = ('application/x-qt-windows-mime;'
                     'value="ExcludeClipboardContentFromMonitorProcessing"')

# Secrets on the clipboard that have not been cleared yet: one clearing
# function each, run by its timer or when the app quits.
_pending_clears: List[Callable[[], None]] = []
_quit_hooked = False
_exit_hooked = False


def secret_mime_data(text: str) -> QMimeData:
    """``text`` as clipboard data marked as a secret."""
    data = QMimeData()
    data.setText(text)
    data.setData(CONCEALED_MAC, b"")
    data.setData(CONCEALED_KDE, b"secret")
    data.setData(CONCEALED_WINDOWS, b"\x00\x00\x00\x00")
    return data


def copy_secret(text: str, *, seconds: Optional[int] = None) -> None:
    """Copy a secret, and clear it from the clipboard after ``seconds``
    (a minute unless given) or when MyEditor quits, unless something else
    was copied since."""
    seconds = SECRET_CLIPBOARD_SECONDS if seconds is None else seconds
    clear_if_unchanged = _put_secret(text)

    def clear() -> None:
        if clear in _pending_clears:
            _pending_clears.remove(clear)
            clear_if_unchanged()

    _pending_clears.append(clear)
    _clear_on_quit()
    QTimer.singleShot(seconds * 1000, clear)


def clear_pending_secrets() -> None:
    """Take every secret still on the clipboard off it now."""
    for clear in list(_pending_clears):
        try:
            clear()
        except RuntimeError:    # the clipboard went with the application
            pass


def _clear_on_quit() -> None:
    """Clear when the app quits, and in any case when Python exits.

    The exit hook covers every way out that skips aboutToQuit, and it
    matters for more than the secret: Qt's clipboard outlives Python, and
    clipboard data made in Python that is still there when Qt tears the
    clipboard down would be freed after the interpreter is gone.
    """
    global _quit_hooked, _exit_hooked
    app = QApplication.instance()
    if not _quit_hooked and app is not None:
        app.aboutToQuit.connect(clear_pending_secrets)
        _quit_hooked = True
    if not _exit_hooked:
        atexit.register(clear_pending_secrets)
        _exit_hooked = True


def _put_secret(text: str) -> Callable[[], None]:
    """Put ``text`` on the clipboard as a secret; return what clears it
    if it is still the clipboard's content."""
    if sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa":
        try:
            return _MacPasteboard().put_secret(text)
        except Exception:  # noqa: BLE001, fall back to Qt's clipboard
            pass
    clipboard = QApplication.clipboard()
    clipboard.setMimeData(secret_mime_data(text))

    def clear_if_unchanged() -> None:
        if clipboard.text() == text:
            clipboard.clear()

    return clear_if_unchanged


class _MacPasteboard:
    """Just enough of NSPasteboard, through the Objective-C runtime, to put
    a concealed string on the pasteboard and clear it while it is unchanged
    (the change count says whether anything was copied since)."""

    def __init__(self, name: Optional[str] = None) -> None:
        import ctypes
        import ctypes.util
        self._c = ctypes
        objc = ctypes.CDLL(ctypes.util.find_library("objc"))
        ctypes.CDLL(ctypes.util.find_library("AppKit"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
        objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
        self._objc = objc
        self._send = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
        self._name = name

    def _msg(self, obj, selector: bytes, *args, restype=None, argtypes=()):
        c = self._c
        function = c.CFUNCTYPE(restype or c.c_void_p, c.c_void_p, c.c_void_p,
                               *argtypes)(self._send)
        return function(obj, self._objc.sel_registerName(selector), *args)

    def _string(self, text: str):
        return self._msg(self._objc.objc_getClass(b"NSString"), b"stringWithUTF8String:",
                         text.encode("utf-8"), argtypes=(self._c.c_char_p,))

    def _pasteboard(self):
        cls = self._objc.objc_getClass(b"NSPasteboard")
        if self._name is None:
            return self._msg(cls, b"generalPasteboard")
        return self._msg(cls, b"pasteboardWithName:", self._string(self._name),
                         argtypes=(self._c.c_void_p,))

    def _in_pool(self, work):
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            return work()
        finally:
            self._objc.objc_autoreleasePoolPop(pool)

    def put_secret(self, text: str) -> Callable[[], None]:
        c = self._c

        def put() -> int:
            board = self._pasteboard()
            self._msg(board, b"clearContents", restype=c.c_long)
            pair = (c.c_void_p, c.c_void_p)
            wrote = self._msg(board, b"setString:forType:", self._string(text),
                              self._string("public.utf8-plain-text"),
                              restype=c.c_bool, argtypes=pair)
            empty = self._msg(self._objc.objc_getClass(b"NSData"), b"data")
            marked = self._msg(board, b"setData:forType:", empty,
                               self._string(CONCEALED_MAC), restype=c.c_bool, argtypes=pair)
            if not (wrote and marked):
                raise OSError("the pasteboard didn't take the secret")
            return self._msg(board, b"changeCount", restype=c.c_long)

        written = self._in_pool(put)

        def clear_if_unchanged() -> None:
            def clear() -> None:
                board = self._pasteboard()
                if self._msg(board, b"changeCount", restype=c.c_long) == written:
                    self._msg(board, b"clearContents", restype=c.c_long)
            self._in_pool(clear)

        return clear_if_unchanged


def _repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class StepList(QWidget):
    """Numbered steps whose state is said in words and shown in colour."""

    _MARKS = {"done": "✓", "error": "!"}

    def __init__(self, steps: Iterable[Tuple[str, str]], parent=None):
        super().__init__(parent)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(10)
        self._rows: Dict[str, dict] = {}
        for number, (key, title) in enumerate(steps, start=1):
            badge = QLabel(str(number))
            badge.setObjectName("step_badge")
            badge.setFixedSize(22, 22)
            badge.setAlignment(Qt.AlignCenter)
            title_label = text_label(title, "item_title")
            detail = text_label("", "muted")
            detail.hide()
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(title_label)
            text.addWidget(detail)
            row = QHBoxLayout()
            row.setSpacing(12)
            row.addWidget(badge, 0, Qt.AlignTop)
            row.addLayout(text, 1)
            column.addLayout(row)
            self._rows[key] = {"number": number, "badge": badge, "title": title_label,
                               "detail": detail, "state": "pending"}
            self.set_state(key, "pending")

    def set_state(self, key: str, state: str, detail: str = "") -> None:
        """``state`` is pending, active, done or error."""
        row = self._rows[key]
        row["state"] = state
        badge = row["badge"]
        badge.setProperty("state", state)
        badge.setText(self._MARKS.get(state, str(row["number"])))
        spoken = {"pending": _("Step {number}, not started"),
                  "active": _("Step {number}, in progress"),
                  "done": _("Step {number}, done"),
                  "error": _("Step {number}, failed")}[state]
        badge.setAccessibleName(spoken.format(number=row["number"]))
        _repolish(badge)
        row["detail"].setText(detail)
        row["detail"].setVisible(bool(detail))

    def state(self, key: str) -> str:
        return self._rows[key]["state"]


class _PullDown(PopupButton):
    """A pull-down button: its title and the Disclosure arrow at its end,
    in the room its padding keeps. Qt sizes a button with a menu for its
    own indicator too, which is not drawn, so that room is given back."""

    def __init__(self, title: str, menu: QMenu, *, is_dark: bool) -> None:
        super().__init__(title)
        self.setObjectName("pull_down")
        self.setMenu(menu)
        Disclosure(self, end=PULL_DOWN_ARROW_END, dark=is_dark)

    def sizeHint(self) -> QSize:  # noqa: N802, Qt's name
        hint = super().sizeHint()
        option = QStyleOptionButton()
        self.initStyleOption(option)
        indicator = self.style().pixelMetric(QStyle.PixelMetric.PM_MenuButtonIndicator,
                                             option, self)
        return QSize(hint.width() - indicator, hint.height())


class _ShownPageStack(QStackedWidget):
    """A stack of pages that asks for the room of the page it shows.

    QStackedWidget asks for the room of its largest page, so a narrow page
    would be laid out as wide as the widest one and cut off at the side of
    a window sized for it. The same holds for the height its wrapped
    text needs at a width, which the scroll area asks for: answered for
    the tallest page, a short one scrolled (a waiting page showed only a
    scroll bar), and a short page after a long one would scroll in a
    window with room to spare."""

    def sizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self) -> QSize:
        page = self.currentWidget()
        return page.minimumSizeHint() if page is not None else super().minimumSizeHint()

    def heightForWidth(self, width: int) -> int:  # noqa: N802, Qt's name
        page = self.currentWidget()
        return page.heightForWidth(width) if page is not None else super().heightForWidth(width)


class AssistantWindow(QDialog):
    """A window of pages with one row of buttons, laid out the Apple way."""

    def __init__(self, title: str, *, is_dark: bool = True, min_width: int = 520,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(min_width)
        self._base_width = min_width
        self.setStyleSheet(stylesheet(is_dark))
        appearance.surface(self, is_dark)
        appearance.bind(self)
        self._is_dark = is_dark
        self._page: Optional[str] = None
        self._pages: Dict[str, QWidget] = {}
        self.buttons: Dict[str, QPushButton] = {}
        self._button_widgets: List[QPushButton] = []
        self._action: Optional[QPushButton] = None
        # Where the focus fell when a button dimmed itself and no action
        # could take it yet (see _on_focus_moved).
        self._fell_to: Optional[QWidget] = None
        QApplication.instance().focusChanged.connect(self._on_focus_moved)

        self._stack = _ShownPageStack()
        # A page taller than the screen scrolls, so the buttons stay in
        # reach; otherwise the window simply grows to fit it.
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # It shows nothing of its own: no Tab stop that would look like
        # the focus went nowhere (a child it scrolls to still gets it).
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setWidget(self._stack)
        # QScrollArea paints its widget's background by default; the page
        # sits on the window's own background, in light and dark alike.
        self._stack.setAutoFillBackground(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(18)
        layout.addWidget(self._scroll, 1)
        self._button_row = QHBoxLayout()
        self._button_row.setSpacing(8)
        layout.addLayout(self._button_row)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = is_dark
        self.setStyleSheet(stylesheet(is_dark))
        appearance.surface(self, is_dark)
        # These are appearance-only children, never page or operation rebuilds.
        from .profile_card import ProfileCard
        from .membership_card import MembershipCard
        from .grouped_list import ListGroup, Notice
        for child in self.findChildren(QWidget):
            if isinstance(child, ProfileCard):
                child.set_dark(is_dark)
            elif isinstance(child, (MembershipCard, ListGroup, Notice)):
                child._is_dark = is_dark
                child.update()

    @property
    def page(self) -> Optional[str]:
        return self._page

    def _on_focus_moved(self, old, new) -> None:
        """A button of this window that dims itself or goes (its row laid
        out again) while it has the focus hands the focus to the window's
        action, the button Return chooses at rest. Qt moves it on to the
        next control instead, a link or Cancel, and on Windows and Linux
        Return would then press that one. While the window has no action to
        take it (Choose File… while a file is read), it is given to the
        next one laid out, or to the action once it is lit again (Save,
        dimmed until the form changes), unless the person has moved the
        focus meanwhile."""
        if new is not self._fell_to:
            self._fell_to = None
        if (isinstance(old, QPushButton) and shiboken6.isValid(old)
                and (not old.isEnabled() or old.isHidden())
                and old.window() is self and new is not None and new.window() is self):
            if self._action_can_take_focus() and self._action is not old:
                self._action.setFocus(Qt.FocusReason.OtherFocusReason)
            else:
                self._fell_to = new

    def _action_can_take_focus(self) -> bool:
        action = self._action
        return (action is not None and shiboken6.isValid(action)
                and action.isEnabled() and not action.isHidden())

    def _give_owed_focus(self) -> None:
        if (self._fell_to is not None and QApplication.focusWidget() is self._fell_to
                and self._action_can_take_focus()):
            self._fell_to = None
            self._action.setFocus(Qt.FocusReason.OtherFocusReason)

    def add_page(self, key: str, widget: QWidget) -> None:
        self._pages[key] = widget
        self._stack.addWidget(widget)
        # A page whose text changed asks for a new layout; it may need
        # more room than before.
        widget.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:
        if (event.type() == QEvent.Type.LayoutRequest
                and watched is self._stack.currentWidget()):
            self.fit_page()
        elif event.type() == QEvent.Type.EnabledChange and watched is self._action:
            self._give_owed_focus()
        return super().eventFilter(watched, event)

    def show_page(self, key: str, buttons: Iterable[ButtonSpec]) -> None:
        self._page = key
        self._stack.setCurrentWidget(self._pages[key])
        self._stack.updateGeometry()
        self.set_buttons(buttons)
        self.fit_page()

    def fit_page(self) -> None:
        """Make room for every line of the page shown.

        Wrapped text needs more height the narrower the window is, and a
        stack of pages does not pass that on, so the page's own height at
        the current width becomes the window's minimum, up to what the
        screen can show; beyond that the page scrolls. Longer words (a
        translation, a larger font) then grow the window instead of
        clipping the text. It runs again whenever the page's layout
        changes, so text that arrives later fits too.

        A page that cannot get narrower than the window (a row of longer
        words) widens the window the same way, and so does a row of buttons
        whose words need more room."""
        page = self._stack.currentWidget()
        layout = page.layout() if page is not None else None
        if layout is None:
            return
        margins = self.layout().contentsMargins()
        sides = margins.left() + margins.right()
        narrowest = layout.totalMinimumSize().width()
        width = self._scroll.viewport().width()
        if not self.isVisible() or width <= 0:
            # Not on screen yet: measured at the width it opens at, its size
            # hint's, unless it was given one. (Its own width is a child's
            # first size until then, or its least width; measured there,
            # the page's words wrapped more than they are shown, and the
            # window opened taller than its page.)
            opens_at = (self.width() if self.testAttribute(Qt.WidgetAttribute.WA_Resized)
                        else self.sizeHint().width())
            width = max(opens_at, self.minimumWidth()) - sides
        width = max(width, narrowest)
        if layout.hasHeightForWidth():
            needed = layout.totalHeightForWidth(width)
        else:
            needed = layout.totalSizeHint().height()
        if needed != self._stack.minimumHeight():
            self._stack.setMinimumHeight(needed)
        shown = min(needed, self._tallest_page())
        if shown != self._scroll.minimumHeight():
            self._scroll.setMinimumHeight(shown)
        # As wide as the page shown needs, plus the scroll bar when it
        # scrolls, so nothing is cut off at the right edge; the window
        # widens with it (its own minimum width keeps it from growing by
        # itself).
        bar = (self._scroll.verticalScrollBar().sizeHint().width() if needed > shown
               else 0)
        wide = self._stack.minimumSizeHint().width() + bar
        if wide != self._scroll.minimumWidth():
            self._scroll.setMinimumWidth(wide)
        # The buttons keep their words whole too: a longer translation of
        # them widens the window rather than cutting them.
        buttons = self._button_row.sizeHint().width()
        window = max(self._base_width, wide + sides, narrowest + sides, buttons + sides)
        if window != self.minimumWidth():
            self.setMinimumWidth(window)
        # A window on screen keeps its size when what it holds needs more
        # (a field problem that widens the page and adds lines at once): it
        # grows to its layout's minimum, or the page runs under the buttons.
        if self.isVisible():
            self.layout().invalidate()
            least = self.layout().totalMinimumSize()
            if self.width() < least.width() or self.height() < least.height():
                self.resize(max(self.width(), least.width()), max(self.height(), least.height()))

    def _tallest_page(self) -> int:
        """The most page height the screen has room for, with the window's
        title bar, margins and buttons around it."""
        area = available_area(self)
        if area is None:
            return 10_000
        margins = self.layout().contentsMargins()
        around = (margins.top() + margins.bottom() + self.layout().spacing()
                  + self._button_row.sizeHint().height() + 60)
        return max(200, area.height() - around)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.fit_page()

    def set_buttons(self, specs: Iterable[ButtonSpec]) -> None:
        """Lay out ``(key, label, placement, handler)`` buttons: Go Back at
        the leading edge, the rest at the trailing edge with the one
        default button last. A menu as the handler makes a pull-down
        button; the menu stays the page's, the button goes with the row."""
        specs = list(specs)
        for widget in self._button_widgets:
            self._button_row.removeWidget(widget)
            # Hidden now, deleted later: a button from the previous page
            # must not stay on screen until the event loop gets to it.
            widget.hide()
            widget.deleteLater()
        self._button_widgets = []
        while self._button_row.count():
            self._button_row.takeAt(0)
        self.buttons = {}
        self._action = None

        def make(key, label, placement, handler):
            if isinstance(handler, QMenu):
                # As a Mac draws a pull-down button (Qt's own indicator is
                # a dot there).
                button = _PullDown(label, handler, is_dark=self._is_dark)
            else:
                button = QPushButton(label)
                button.clicked.connect(handler)
            # Return as the system's own dialogs answer it (alerts.py).
            answer_return_as_the_system_does([button])
            button.setDefault(placement == DEFAULT)
            if placement == DEFAULT:
                self._action = button
                button.installEventFilter(self)     # lit again: see _give_owed_focus
            self.buttons[key] = button
            self._button_widgets.append(button)
            return button

        for spec in specs:
            if spec[2] == LEADING:
                self._button_row.addWidget(make(*spec))
        self._button_row.addStretch(1)
        for placement in (NORMAL, DEFAULT):
            for spec in specs:
                if spec[2] == placement:
                    self._button_row.addWidget(make(*spec))
        # Once the new row is on screen (a button added to a shown window
        # shows a moment later).
        QTimer.singleShot(0, self, self._give_owed_focus)
        self.fit_page()             # the new buttons may need a wider window
