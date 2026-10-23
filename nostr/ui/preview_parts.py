# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The parts a post's preview is made of, shared by the popover beside the
panel's lists (drafts_preview.py) and the window it is detached into
(reader_window.py), so the two look alike: the header's labels, the cover,
the text styled as Nostr shows it, the bar of commands, and the type and
colour they are drawn in.
"""

from __future__ import annotations

from typing import Callable, List, NamedTuple, Optional, Tuple

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap, QTextDocument, QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QMenu, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from . import appearance
from i18n import _

from doc_walk import iter_image_names
from ..preview import NostrPreview
from .drafts_common import (
    BASE_POINT_SIZE, THEME_TOKENS, app_point_size, scaled, secondary_font,
)

# 14 px on all four sides, more than the panel's 12 px GUTTER. A floating
# surface needs more air than an inline row, and the popover is not
# aligned to the panel's gutter anyway, so matching it would be a
# coincidence rather than an alignment.
PAD: int = 14
RADIUS: int = 8


# What a cover's box says while its picture comes, and when it can't.
IMAGE_WAITING = _("Loading image…")
IMAGE_UNAVAILABLE = _("Image unavailable")


# --------------------------------------------------------------------------- #
# Style                                                                       #
# --------------------------------------------------------------------------- #

def preview_css(is_dark: bool) -> str:
    """One QSS template for both themes, from the panel's token table.

    No ``font-size`` here, for the same reason the panel's sheet carries
    none: Qt treats both ``px`` and ``pt`` in a stylesheet as absolute
    and discards ``em``, so a size set here would freeze while the rest
    of the app scaled around it. QSS carries colour and weight only.

    Every pair is measured in both appearances. Title ``row_fg`` on
    ``chrome_bg`` is 15.31:1 dark and 16.39:1 light, summary
    ``chrome_fg`` 9.54 and 11.90, the metadata lines ``muted`` 4.74 and
    4.87, the expiry ``error_fg`` 6.24 and 4.66, and the image box label
    ``chrome_fg`` on ``border`` 6.87 and 9.66. ``accessibility.md``:
    "Text size / Text weight / Minimum contrast ratio / Up to 17 pts /
    All / 4.5:1", checked "in both light and dark appearances".
    """
    t = THEME_TOKENS[bool(is_dark)]
    return f"""
QLabel#drafts_preview_title {{ color: {t["row_fg"]}; font-weight: 600; }}
QLabel#drafts_preview_summary {{ color: {t["chrome_fg"]}; }}
QLabel#drafts_preview_origin,
QLabel#drafts_preview_stats,
QLabel#drafts_preview_tags {{ color: {t["muted"]}; }}
/* The word "Expires" carries the meaning and the colour only reinforces
   it. accessibility.md: "Convey information with more than color
   alone." */
QLabel#drafts_preview_expiry {{ color: {t["error_fg"]}; }}
QLabel#drafts_preview_image_note {{ color: {t["chrome_fg"]}; }}
"""


def title_font() -> QFont:
    """Title 3 from the macOS text-style table: 15 pt at a 13 pt base.

    A ratio of the application font rather than a literal, so the
    hierarchy survives a user who has enlarged type. ``typography.md``:
    "In general, avoid light font weights." Nothing here is lighter than
    Regular; hierarchy is carried by size, weight and the muted token.
    """
    font = QFont(QApplication.font())
    font.setPointSizeF(app_point_size() * 15.0 / BASE_POINT_SIZE)
    font.setWeight(QFont.DemiBold)
    return font


def body_font() -> QFont:
    """Body from the same table: the application font, unmodified."""
    return QFont(QApplication.font())


class PreviewFields(NamedTuple):
    """What one preview shows, already composed, in reading order.

    Every field is a string and a missing one is empty, never a
    placeholder: the popover drops the line rather than printing "no
    summary". ``layout.md``: "Make essential information easy to find by
    giving it sufficient space. People want to view the most important
    information right away, so don't obscure it by crowding it with
    nonessential details."
    """

    title: str
    summary: str
    origin: str
    stats: str
    tags: str
    expiry: str
    image_url: str


# --------------------------------------------------------------------------- #
# Labels and image                                                            #
# --------------------------------------------------------------------------- #

class ClampedLabel(QLabel):
    """Word-wrapped label capped at N lines, eliding the last one.

    Not a ``QLabel`` with ``wordWrap`` and a fixed height: that hard
    clips the last line with no sign anything was cut, which is exactly
    the bug ``_ElidingLabel`` exists to prevent and which produced a row
    reading ``![One Class, One`` with no trailing dots.

    Laid out at paint time from ``contentsRect().width()`` for the same
    reason ``_ElidingLabel`` elides there: eliding through ``setText``
    changes the size hint, which re-runs the layout, which moves the
    width the elision was computed against.

    The height is fixed by :meth:`apply` from the width the popover was
    composed at, so the surface can be measured before it is shown and
    never resizes underneath a reader.
    """

    def __init__(self, object_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self._full = ""
        self._max_lines = 1
        self._width = 1
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def full_text(self) -> str:
        return self._full

    def max_lines(self) -> int:
        return self._max_lines

    def apply(self, text: str, *, width: int, max_lines: int) -> int:
        """Set the text and fix the height. Returns the height taken."""
        self._full = text or ""
        self._max_lines = max(1, int(max_lines))
        self._width = max(1, int(width))
        # The painted string may be elided and ``text()`` stays empty, so
        # the accessible name is where the whole string stays reachable.
        self.setAccessibleName(self._full)
        height = len(self._ranges(self._width)[0]) * self.fontMetrics().lineSpacing()
        self.setFixedHeight(height)
        self.setVisible(bool(self._full))
        self.update()
        return height

    def line_count(self) -> int:
        return len(self._ranges(self._width)[0])

    def is_truncated(self) -> bool:
        return self._ranges(self._width)[1]

    def painted_lines(self) -> List[str]:
        """Exactly the strings ``paintEvent`` will draw, ellipsis and all."""
        width = max(1, self.contentsRect().width() or self._width)
        ranges, truncated = self._ranges(width)
        out = []
        for index, (start, length) in enumerate(ranges):
            if truncated and index == len(ranges) - 1:
                out.append(self.fontMetrics().elidedText(
                    self._full[start:], Qt.ElideRight, width,
                ))
            else:
                out.append(self._full[start:start + length])
        return out

    def _ranges(self, width: int) -> Tuple[List[Tuple[int, int]], bool]:
        if not self._full:
            return [], False
        layout = QTextLayout(self._full, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        ranges: List[Tuple[int, int]] = []
        truncated = False
        while len(ranges) < self._max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            ranges.append((line.textStart(), line.textLength()))
        else:
            truncated = layout.createLine().isValid()
        layout.endLayout()
        return ranges, truncated

    def paintEvent(self, event) -> None:
        if not self._full:
            return
        painter = QPainter(self)
        painter.setFont(self.font())
        # Through the palette, which is where the stylesheet's ``color``
        # lands after a polish, so the token table stays the only source
        # of colour here as well.
        painter.setPen(QPen(self.palette().color(self.foregroundRole())))
        rect = self.contentsRect()
        metrics = self.fontMetrics()
        y = rect.top() + metrics.ascent()
        for text in self.painted_lines():
            painter.drawText(rect.left(), y, text)
            y += metrics.lineSpacing()


class HeroImage(QWidget):
    """The 16:9 hero box, whose geometry never depends on the bytes.

    A fixed box rather than the image's own ratio, so frames one, two
    and three are identical in size: reserved and empty, reserved and
    labelled, and filled. Nothing below it ever moves, and the window is
    never resized after it is shown.

    ``layout.md``: "When necessary, scale artwork in response to display
    changes... don't change the aspect ratio of the artwork; instead,
    scale it so that important visual content remains visible."
    Expanding plus a centre crop preserves the ratio and keeps the
    middle, which is where a hero image puts its subject.

    No spinner, no shimmer, no skeleton animation. Qt exposes no
    portable reduce-motion query, so the only safe design is one that
    needs no toggle: a static fill and a word.
    """

    def __init__(self, parent: Optional[QWidget] = None, *, is_dark: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_preview_image")
        self._is_dark = bool(is_dark)
        self._pixmap: Optional[QPixmap] = None
        # NIP-23 carries no alt text, so the name says what the thing is
        # and no description invents what it depicts.
        self.setAccessibleName(_("Article image"))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(PAD, PAD, PAD, PAD)
        self._note = QLabel("", self)
        self._note.setObjectName("drafts_preview_image_note")
        self._note.setAlignment(Qt.AlignCenter)
        self._note.setWordWrap(True)
        self._note.hide()
        layout.addWidget(self._note)

    def note(self) -> QLabel:
        return self._note

    def note_text(self) -> str:
        return self._note.text() if self._note.isVisibleTo(self) else ""

    def pixmap(self) -> Optional[QPixmap]:
        """The cover as it is drawn, cropped to the box, or None."""
        return self._pixmap

    def has_pixmap(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    def reset(self, *, width: int) -> None:
        self._pixmap = None
        self._note.setText("")
        self._note.setFont(secondary_font())
        self._note.hide()
        pad = scaled(PAD)
        self.layout().setContentsMargins(pad, pad, pad, pad)
        self.setFixedSize(width, round(width * 9 / 16))
        self.update()

    def set_note(self, text: str) -> None:
        """Say what is happening in the reserved box, without resizing it."""
        self._note.setText(text)
        self._note.setVisible(bool(text) and not self.has_pixmap())
        self.update()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap is None or pixmap.isNull():
            return
        box = self.size()
        # ``images.md``: "High-resolution 2D displays have higher pixel
        # densities, such as 2:1 or 3:1." A hero rendered at 1x on a 2x
        # display is visibly soft next to the crisp text beside it.
        dpr = float(self.devicePixelRatioF() or 1.0)
        target = QSize(
            max(1, round(box.width() * dpr)), max(1, round(box.height() * dpr)),
        )
        grown = pixmap.scaled(
            target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        x = max(0, (grown.width() - target.width()) // 2)
        y = max(0, (grown.height() - target.height()) // 2)
        cropped = grown.copy(x, y, target.width(), target.height())
        cropped.setDevicePixelRatio(dpr)
        self._pixmap = cropped
        self._note.hide()
        self.update()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()
        # Top corners follow the surface; the bottom ones are square
        # because the text block continues straight below.
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom() + 1)
        path.lineTo(rect.left(), rect.top() + RADIUS)
        path.quadTo(rect.left(), rect.top(), rect.left() + RADIUS, rect.top())
        path.lineTo(rect.right() + 1 - RADIUS, rect.top())
        path.quadTo(
            rect.right() + 1, rect.top(), rect.right() + 1, rect.top() + RADIUS,
        )
        path.lineTo(rect.right() + 1, rect.bottom() + 1)
        path.closeSubpath()
        painter.setClipPath(path)
        # The fill is deliberately quiet: ``border`` on ``chrome_bg`` is
        # 1.39:1 dark and 1.23:1 light, and a placeholder fill is not
        # text. The label on top of it is what carries meaning, at
        # 6.87:1 and 9.66:1.
        painter.fillRect(rect, QColor(THEME_TOKENS[self._is_dark]["border"]))
        if self.has_pixmap():
            painter.drawPixmap(rect, self._pixmap)


# --------------------------------------------------------------------------- #
# The full preview's text and commands                                        #
# --------------------------------------------------------------------------- #

class PreviewAction(NamedTuple):
    """A command at the foot of a full preview, as its host offers it.

    ``primary`` is the next step (Continue Editing, Edit Article), drawn
    as the default button at the trailing end; ``menu`` makes a pull-down
    of ``(key, title)`` choices ("Open in"); ``more`` puts the command in
    the ⋯ menu instead of on the bar (Delete last, as menus order it).
    """

    key: str
    title: str
    primary: bool = False
    menu: Tuple[Tuple[str, str], ...] = ()
    more: bool = False
    destructive: bool = False


class PreviewReader(NostrPreview):
    """The text of a full preview, as Nostr shows it (the editor's
    Preview renderer), on the popover's own ground with a line above it.
    Its pictures, and its audio and video cards, come from the lookup the
    window gives it (set_pictures), as the editor's Preview finds them;
    without one they show as frames, and nothing is fetched for them."""

    def __init__(self, *, dark: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(dark=dark, parent=parent)
        self._arrived = None
        self.setObjectName("drafts_preview_reader")
        self.setAccessibleName(_("Text"))
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        # A step smaller than the editor's Preview: a popover is a glance
        # at the text, beside the document being written.
        self.zoomOut(2)

    def _apply_colors(self) -> None:
        # Document text uses the same neutral reading ground as the editor.
        # The enclosing preview header and action bar own the Nostr accent.
        super()._apply_colors()
        appearance.neutral(self, self._dark)

    def show_markdown(self, markdown, *, names=None) -> None:
        self._source = ("show_markdown", markdown, {"names": names})
        super().show_markdown(markdown, names=names)

    def show_note(self, text, *, author="", names=None) -> None:
        self._source = ("show_note", text, {"author": author, "names": names})
        super().show_note(text, author=author, names=names)

    def set_dark(self, dark: bool) -> None:
        changed = self._dark != dark
        cursor = self.textCursor()
        anchor, position = cursor.anchor(), cursor.position()
        scroll = self.verticalScrollBar().value()
        super().set_dark(dark)
        if changed and hasattr(self, "_source"):
            # Rendered documents contain explicit text/link colors. Changing
            # only the widget palette leaves pale Dark text on a Light page.
            method, text, options = self._source
            getattr(self, method)(text, **options)
            cursor = self.textCursor()
            cursor.setPosition(anchor)
            cursor.setPosition(position, cursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)
            self.verticalScrollBar().setValue(scroll)

    def set_pictures(self, lookup: Optional[Callable[[str], Optional[QImage]]],
                     arrived=None) -> None:
        """Find the text's pictures with ``lookup`` (a picture by its name,
        None while it is on its way); ``arrived``, a signal naming a picture
        that came, puts it in its frame. None: frames only."""
        if self._arrived is not None:
            try:
                self._arrived.disconnect(self._picture_arrived)
            except (RuntimeError, TypeError):
                pass
        self._images, self._arrived = lookup, arrived
        if arrived is not None:
            arrived.connect(self._picture_arrived)

    def _picture_arrived(self, name: str) -> None:
        doc = self.document()
        if self._images is None or name not in set(iter_image_names(doc)):
            return
        image = self._images(name)
        if image is not None:
            doc.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), image)
            doc.markContentsDirty(0, doc.characterCount())

    def text_height(self, width: int) -> int:
        """How tall the text is laid out at ``width``, margins included:
        measured at the width it is really given (the reader's own column
        and margins), so a text that fits shows no scrollbar."""
        self.setFixedWidth(width)
        self._center_column()
        margins = self.viewportMargins()
        column = max(120, width - margins.left() - margins.right()
                     - 2 * self.frameWidth())
        doc = self.document()
        doc.setTextWidth(column)
        return int(doc.size().height()) + margins.top() + margins.bottom() + 4


class ActionBar(QWidget):
    """The foot of a full preview: the ⋯ menu leading, the commands, and
    the primary one trailing as the default button (Apple's guidelines put
    the default action on the trailing side). ``chosen(key)``."""

    chosen = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, *, is_dark: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_preview_bar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._is_dark = bool(is_dark)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(10, 8, 10, 8)
        self._row.setSpacing(8)
        self.buttons: dict = {}
        self.primary: Optional[QWidget] = None
        self.more: Optional[QToolButton] = None

    def set_actions(self, actions: Tuple[PreviewAction, ...]) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.buttons, self.primary, self.more = {}, None, None
        # Destructive commands last, after a line, as menus order them.
        extra = sorted((action for action in actions if action.more),
                       key=lambda action: action.destructive)
        if extra:
            self.more = QToolButton()
            self.more.setObjectName("drafts_preview_more")
            self.more.setText("\u22ef")
            self.more.setAccessibleName(_("More"))
            self.more.setToolTip(_("More"))
            self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            menu = QMenu(self.more)
            for index, action in enumerate(extra):
                if action.destructive and index:
                    menu.addSeparator()
                entry = menu.addAction(action.title)
                entry.setData(action.key)
            menu.triggered.connect(self._on_menu)
            self.more.setMenu(menu)
            self._row.addWidget(self.more)
        self._row.addStretch(1)
        on_bar = [action for action in actions if not action.more]
        for action in sorted(on_bar, key=lambda a: a.primary):
            if action.menu:
                button = QToolButton()
                button.setText(action.title + "  \u25be")
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                menu = QMenu(button)
                for key, title in action.menu:
                    entry = menu.addAction(title)
                    entry.setData(key)
                menu.triggered.connect(self._on_menu)
                button.setMenu(menu)
            else:
                button = QPushButton(action.title)
                button.setAutoDefault(False)
                button.clicked.connect(lambda _checked=False, k=action.key: self.chosen.emit(k))
            button.setObjectName("drafts_preview_primary" if action.primary
                                 else "drafts_preview_action")
            button.setAccessibleName(action.title)
            self._row.addWidget(button)
            self.buttons[action.key] = button
            if action.primary:
                self.primary = button
        self.apply_theme(self._is_dark)

    def _on_menu(self, entry) -> None:
        key = entry.data()
        if isinstance(key, str) and key:
            self.chosen.emit(key)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        t = THEME_TOKENS[self._is_dark]
        self.setStyleSheet(f"""
QWidget#drafts_preview_bar {{ background: {t["chrome_bg"]};
    border-top: 1px solid {t["border"]}; }}
QPushButton#drafts_preview_action, QToolButton#drafts_preview_action,
QToolButton#drafts_preview_more {{
    background: transparent; color: {t["row_fg"]};
    border: 1px solid {t["border"]}; border-radius: 6px; padding: 4px 12px; }}
QToolButton#drafts_preview_more {{ padding: 4px 8px; }}
QToolButton#drafts_preview_action::menu-indicator,
QToolButton#drafts_preview_more::menu-indicator {{ image: none; width: 0px; }}
QPushButton#drafts_preview_action:hover, QToolButton#drafts_preview_action:hover,
QToolButton#drafts_preview_more:hover {{ background: {t["hover_bg"]}; }}
QPushButton#drafts_preview_primary {{ border-radius: 6px; padding: 4px 14px; }}
{appearance.accent_button_css("QPushButton#drafts_preview_primary", self._is_dark)}
QPushButton:focus, QToolButton:focus {{ border: 2px solid {appearance.roles(self._is_dark).focus}; }}
""")
