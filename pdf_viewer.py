#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Distraction-free PDF reading tab built on Qt's native QPdfView.

Design brief:
  - Reading, not editing. One slim toolbar (contents toggle, page
    indicator, zoom), an on-demand find bar reusing the editor's
    FindBar widget, and the document. Nothing else competes for
    attention.
  - Keyboard-first, with the muscle memory readers bring over from
    SumatraPDF: Space / Shift+Space page through like a browser, j / k
    scroll by line, n / p step pages, g focuses the go-to-page box,
    Home / End jump to the first / last page, Ctrl+Plus / Ctrl+Minus
    and Ctrl+wheel zoom, Ctrl+0 fits width, Ctrl+1 is actual size,
    Ctrl+2 fits the whole page, F12 toggles the contents outline,
    Ctrl+F finds.
  - The last reading position (page + zoom) is remembered per file in
    ``pdf_positions.json`` so reopening a book drops you where you left
    off. The store is LRU-capped so it never grows unbounded.
  - Theme-aware chrome. Pages themselves render as authored (white);
    only the surrounding surface follows the app theme, the same
    honesty as the PDF exporter forcing a white page.

Search relies on QPdfSearchModel's lazy background scan: ``count()``
grows as pages are examined and ``countChanged`` fires along the way,
so the match label updates live without blocking on large documents.
"""

import json
import os
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QGuiApplication, QIntValidator, QKeySequence,
    QPainter, QPalette, QShortcut, QTransform,
)
from PySide6.QtPdf import QPdfBookmarkModel, QPdfDocument, QPdfLinkModel, QPdfSearchModel
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QToolButton,
    QTreeView, QVBoxLayout, QWidget,
)

from url_safety import is_safe_external_url
from widgets import FindBar

_POSITIONS_PATH = os.path.expanduser("~/.config/my_editor/pdf_positions.json")

# Reading positions kept for this many distinct files before the oldest
# entry is evicted. Generous for a personal library, tiny on disk.
_MAX_POSITIONS = 100

_ZOOM_STEP = 1.2
_MIN_ZOOM = 0.2
_MAX_ZOOM = 8.0


# --------------------------------------------------------------------------- #
# Reading-position store                                                      #
# --------------------------------------------------------------------------- #

def _load_positions() -> dict:
    try:
        with open(_POSITIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def load_view_state(path: str) -> dict | None:
    """Return the stored view state for ``path`` (page, zoom), or None."""
    state = _load_positions().get(os.path.abspath(path))
    return state if isinstance(state, dict) else None


def save_view_state(path: str, state: dict) -> None:
    """Persist the view state for ``path``, evicting the oldest entries
    beyond the cap. Failures are swallowed: losing a reading position
    must never interfere with closing a tab or quitting."""
    try:
        positions = _load_positions()
        state = dict(state, ts=int(time.time()))
        positions[os.path.abspath(path)] = state
        if len(positions) > _MAX_POSITIONS:
            oldest_first = sorted(positions.items(),
                                  key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0)
            positions = dict(oldest_first[len(positions) - _MAX_POSITIONS:])
        os.makedirs(os.path.dirname(_POSITIONS_PATH), exist_ok=True)
        with open(_POSITIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(positions, f)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Toolbar stylesheets (aligned with FindBar / header greys)                   #
# --------------------------------------------------------------------------- #

_TOOLBAR_DARK_CSS = """
#PdfToolbar { background: #252526; border-bottom: 1px solid #3C3C3C; }
QLabel { color: #CCCCCC; font-size: 12px; background: transparent; }
QLineEdit {
    background: #1E1E1E; color: #D4D4D4;
    border: 1px solid #3C3C3C; border-radius: 4px;
    padding: 2px 4px; font-size: 12px;
    selection-background-color: #264F78;
}
QToolButton {
    background: transparent; color: #D4D4D4;
    border: 1px solid transparent; border-radius: 4px;
    padding: 3px 8px; font-size: 12px;
}
QToolButton:hover { background: #3C3C3C; }
QToolButton:checked { background: #3C2800; color: #FFB347; border-color: #FF8C00; }
"""

_TOOLBAR_LIGHT_CSS = """
#PdfToolbar { background: #F8F8F8; border-bottom: 1px solid #E1E1E1; }
QLabel { color: #333333; font-size: 12px; background: transparent; }
QLineEdit {
    background: #FFFFFF; color: #333333;
    border: 1px solid #E1E1E1; border-radius: 4px;
    padding: 2px 4px; font-size: 12px;
    selection-background-color: #0078D4;
}
QToolButton {
    background: transparent; color: #333333;
    border: 1px solid transparent; border-radius: 4px;
    padding: 3px 8px; font-size: 12px;
}
QToolButton:hover { background: #E1E1E1; }
QToolButton:checked { background: #FFF0D0; color: #A05000; border-color: #E88000; }
"""

# Surface painted around the pages. Slightly darker than the page in
# light mode, the app backdrop in dark mode, so pages read as sheets.
_VIEW_BG_DARK = QColor("#1E1E1E")
_VIEW_BG_LIGHT = QColor("#E8E8E8")

_OUTLINE_WIDTH = 260

_OUTLINE_DARK_CSS = """
QTreeView {
    background: #252526; color: #CCCCCC;
    border: none; border-right: 1px solid #3C3C3C;
    font-size: 12px; outline: none;
}
QTreeView::item { padding: 3px 4px; }
QTreeView::item:hover { background: #2D2D30; }
QTreeView::item:selected { background: #264F78; color: #FFFFFF; }
"""

_OUTLINE_LIGHT_CSS = """
QTreeView {
    background: #F8F8F8; color: #333333;
    border: none; border-right: 1px solid #E1E1E1;
    font-size: 12px; outline: none;
}
QTreeView::item { padding: 3px 4px; }
QTreeView::item:hover { background: #ECECEC; }
QTreeView::item:selected { background: #0078D4; color: #FFFFFF; }
"""


# Painted over selected text. Pages always render white (see module
# docstring), so one color works for both app themes.
_SELECTION_FILL = QColor(0, 120, 212, 70)


class _PageTextIndex:
    """Per-page glyph geometry for caret snapping.

    QPdfDocument.getSelection() places both endpoints with a small
    tolerance and returns nothing when either lands in whitespace,
    which makes raw drag-selection die in margins and line gaps. Real
    readers snap the endpoint to the nearest character instead. One
    whole-page getSelectionAtIndex() call returns a polygon per
    non-whitespace character in text order (verified against pdfium),
    so the entire snapping index costs a single call (~1.5 ms) rather
    than one call per character (~1.4 s).
    """

    def __init__(self, document, page: int):
        text = document.getAllText(page).text()
        self.char_count = len(text)
        glyph_chars = [i for i, c in enumerate(text) if not c.isspace()]
        polys = document.getSelectionAtIndex(page, 0, self.char_count).bounds()
        # Defensive clamp in case pdfium's whitespace notion ever
        # disagrees with str.isspace() for some exotic character.
        n = min(len(glyph_chars), len(polys))
        self._chars = glyph_chars[:n]
        self._rects = [p.boundingRect() for p in polys[:n]]

    def caret_at(self, point: QPointF):
        """Caret position (0..char_count) nearest to a page point, or
        None when the page has no text. Distance is lexicographic
        (vertical first): the line at the cursor's height always wins,
        and horizontal distance only picks the character within it.
        Anything else lets a longer line above or below capture a drag
        into the margin."""
        if not self._rects:
            return None
        best, best_score = 0, (float("inf"), float("inf"))
        for i, r in enumerate(self._rects):
            dx = max(r.left() - point.x(), 0.0, point.x() - r.right())
            dy = max(r.top() - point.y(), 0.0, point.y() - r.bottom())
            score = (dy, dx)
            if score < best_score:
                best, best_score = i, score
        char = self._chars[best]
        if point.x() > self._rects[best].center().x():
            char += 1
        return char


class _ReaderView(QPdfView):
    """QPdfView plus the two things its widget API lacks: text
    selection and working hyperlinks.

    The widget exposes no viewport-to-page mapping, so this class
    mirrors QPdfViewPrivate::calculateDocumentLayout (Qt 6.11) exactly,
    Qt types and rounding included: content coordinates are viewport
    coordinates plus the scroll offsets, pages stack vertically inside
    documentMargins with pageSpacing between them and center
    horizontally, and page points scale to pixels by
    (logicalDotsPerInch / 72) * pageScale.

    The base class ships link handling behind this same math, but its
    hit test forgets the scroll offset (compares viewport coordinates
    against content coordinates), so it only works before the first
    scroll; it also ignores external URLs. Both are fixed here, which
    is why the mouse handlers deliberately do not call super().
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._link_model = QPdfLinkModel(self)
        self._text_indexes: dict[int, _PageTextIndex] = {}
        # Current selection as [(page, QPdfSelection)]. Selections are
        # stored in page-point space, so they survive zoom and scroll
        # unchanged; painting maps them to pixels on the fly.
        self._selections = []
        self._sel_anchor = None      # (page, caret) where the drag started
        self._press_pos = None
        self._press_link = None
        self._dragging = False
        self.documentChanged.connect(self._link_model.setDocument)
        # Hover feedback (pointing hand over links) needs move events
        # without a button held.
        self.viewport().setMouseTracking(True)

    # -- geometry (mirror of QPdfView's private layout) --------------------

    @staticmethod
    def _screen_resolution() -> float:
        screen = QGuiApplication.primaryScreen()
        return screen.logicalDotsPerInch() / 72.0 if screen else 1.0

    def _page_layouts(self) -> dict:
        """{page: (QRect in content coordinates, page scale)}."""
        doc = self.document()
        if doc is None or doc.status() != QPdfDocument.Status.Ready:
            return {}
        res = self._screen_resolution()
        viewport = self.viewport().size()
        margins = self.documentMargins()
        spacing = self.pageSpacing()
        single = self.pageMode() == QPdfView.PageMode.SinglePage
        pages = ([self.pageNavigator().currentPage()] if single
                 else range(doc.pageCount()))

        sizes = {}
        total_width = 0
        for page in pages:
            if self.zoomMode() == QPdfView.ZoomMode.Custom:
                scale = self.zoomFactor()
                size = (doc.pagePointSize(page) * res * scale).toSize()
            elif self.zoomMode() == QPdfView.ZoomMode.FitToWidth:
                size = (doc.pagePointSize(page) * res).toSize()
                scale = (viewport.width() - margins.left() - margins.right()) / size.width()
                size = size * scale
            else:  # FitInView
                avail = QSize(viewport.width() - margins.left() - margins.right(),
                              viewport.height() - spacing)
                size = (doc.pagePointSize(page) * res).toSize()
                scaled = size.scaled(avail, Qt.KeepAspectRatio)
                scale = scaled.width() / size.width()
                size = scaled
            sizes[page] = (size, scale)
            total_width = max(total_width, size.width())
        total_width += margins.left() + margins.right()

        layouts = {}
        y = margins.top()
        for page in pages:
            size, scale = sizes[page]
            x = (max(total_width, viewport.width()) - size.width()) // 2
            layouts[page] = (QRect(QPoint(x, y), size), scale)
            y += size.height() + spacing
        return layouts

    def _scroll_offset(self) -> QPoint:
        return QPoint(self.horizontalScrollBar().value(),
                      self.verticalScrollBar().value())

    def page_point_at(self, viewport_pos: QPointF):
        """(page, point-in-page-points) under a viewport position, or
        (None, None) when the position is off every page."""
        content = viewport_pos + QPointF(self._scroll_offset())
        for page, (rect, scale) in self._page_layouts().items():
            if rect.contains(content.toPoint()):
                factor = self._screen_resolution() * scale
                return page, (content - QPointF(rect.topLeft())) / factor
        return None, None

    def _page_point_near(self, viewport_pos: QPointF):
        """Like page_point_at, but clamps positions in margins or page
        gaps onto the nearest page so drag-selection never dies there."""
        page, point = self.page_point_at(viewport_pos)
        if page is not None:
            return page, point
        content = viewport_pos + QPointF(self._scroll_offset())
        best = None
        best_dist = float("inf")
        for pg, (rect, scale) in self._page_layouts().items():
            dx = max(rect.left() - content.x(), 0.0, content.x() - rect.right())
            dy = max(rect.top() - content.y(), 0.0, content.y() - rect.bottom())
            dist = dx * dx + dy * dy
            if dist < best_dist:
                clamped = QPointF(min(max(content.x(), rect.left()), rect.right()),
                                  min(max(content.y(), rect.top()), rect.bottom()))
                factor = self._screen_resolution() * scale
                best = (pg, (clamped - QPointF(rect.topLeft())) / factor)
                best_dist = dist
        return best if best is not None else (None, None)

    # -- selection ---------------------------------------------------------

    def _text_index(self, page: int) -> _PageTextIndex:
        index = self._text_indexes.get(page)
        if index is None:
            index = _PageTextIndex(self.document(), page)
            self._text_indexes[page] = index
        return index

    def _caret_near(self, viewport_pos: QPointF):
        page, point = self._page_point_near(viewport_pos)
        if page is None:
            return None
        caret = self._text_index(page).caret_at(point)
        return None if caret is None else (page, caret)

    def _update_selection(self, viewport_pos: QPointF):
        current = self._caret_near(viewport_pos)
        if self._sel_anchor is None or current is None:
            return
        start, end = sorted((self._sel_anchor, current))
        doc = self.document()
        selections = []
        for page in range(start[0], end[0] + 1):
            first = start[1] if page == start[0] else 0
            last = end[1] if page == end[0] else self._text_index(page).char_count
            if last > first:
                selection = doc.getSelectionAtIndex(page, first, last - first)
                if selection.isValid():
                    selections.append((page, selection))
        self._selections = selections
        self.viewport().update()

    def has_selection(self) -> bool:
        return bool(self._selections)

    def selected_text(self) -> str:
        return "\n".join(sel.text() for _, sel in self._selections)

    def clear_selection(self):
        if self._selections:
            self._selections = []
            self.viewport().update()

    def copy_selection(self):
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def reset_reader_state(self):
        """Drop caches tied to document content; call after a reload."""
        self._text_indexes.clear()
        self._sel_anchor = None
        self.clear_selection()

    # -- links -------------------------------------------------------------

    def _link_at(self, viewport_pos: QPointF):
        page, point = self.page_point_at(viewport_pos)
        if page is None:
            return None
        self._link_model.setPage(page)
        link = self._link_model.linkAt(point)
        # QPdfLink.isValid() literally means page() >= 0, so a URL-only
        # link reports invalid even when hit. Treat either destination
        # kind as a hit; a miss returns a link with neither.
        if link.isValid() or not link.url().isEmpty():
            return link
        return None

    def _follow_link(self, link):
        url = link.url()
        if url.isValid() and not url.isEmpty():
            # A PDF is authored entirely by whoever sent it, so its links
            # are handed to the OS only when they are ordinary web links.
            if is_safe_external_url(url.toString()):
                QDesktopServices.openUrl(url)
        elif link.page() >= 0:
            self.pageNavigator().jump(link)

    # -- mouse -------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position()
            self._press_link = self._link_at(event.position())
            self._dragging = False
            self.clear_selection()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and self._press_pos is not None:
            started = (event.position() - self._press_pos).manhattanLength() \
                >= QApplication.startDragDistance()
            if not self._dragging and started:
                self._dragging = True
                self._sel_anchor = self._caret_near(self._press_pos)
            if self._dragging:
                self._update_selection(event.position())
            return
        self.setCursor(Qt.PointingHandCursor if self._link_at(event.position())
                       else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._dragging and self._press_link is not None:
                self._follow_link(self._press_link)
            self._press_pos = None
            self._press_link = None
            self._dragging = False

    # -- painting ----------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._selections:
            return
        layouts = self._page_layouts()
        offset = self._scroll_offset()
        painter = QPainter(self.viewport())
        painter.translate(-offset.x(), -offset.y())
        painter.setPen(Qt.NoPen)
        painter.setBrush(_SELECTION_FILL)
        for page, selection in self._selections:
            layout = layouts.get(page)
            if layout is None:
                continue
            rect, scale = layout
            factor = self._screen_resolution() * scale
            transform = QTransform().translate(rect.x(), rect.y()).scale(factor, factor)
            for poly in selection.bounds():
                painter.drawPolygon(transform.map(poly))
        painter.end()


class PdfViewerTab(QWidget):
    """A read-only PDF tab: toolbar + find bar + QPdfView.

    Exposes ``_file_path`` like the editor widgets so MainWindow's
    path-based bookkeeping (already-open check, session save, watcher)
    treats both tab kinds uniformly.
    """

    # Emitted whenever the visible page changes so the host window can
    # refresh its status bar for the active tab.
    page_changed = Signal()

    def __init__(self, path: str, is_dark: bool = True, parent=None):
        super().__init__(parent)
        self._file_path = path
        self.is_dark = is_dark
        self.load_ok = False
        self.load_error = ""
        self._reload_pending = False

        self.document = QPdfDocument(self)

        self.view = _ReaderView()
        self.view.setDocument(self.document)
        self.view.setPageMode(QPdfView.PageMode.MultiPage)
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)

        self._search = QPdfSearchModel(self)
        self._search.setDocument(self.document)
        self.view.setSearchModel(self._search)
        self._search.countChanged.connect(self._update_match_label)
        # Index into the search results, -1 while nothing is selected.
        self._current_result = -1

        # Table of contents (PDF outline). Hidden until toggled, and the
        # toggle stays disabled for documents that carry no outline.
        self._bookmarks = QPdfBookmarkModel(self)
        self._bookmarks.setDocument(self.document)
        self.outline = QTreeView()
        self.outline.setModel(self._bookmarks)
        self.outline.setHeaderHidden(True)
        self.outline.setFixedWidth(_OUTLINE_WIDTH)
        self.outline.setExpandsOnDoubleClick(False)
        self.outline.activated.connect(self._on_outline_activated)
        self.outline.clicked.connect(self._on_outline_activated)
        self.outline.setVisible(False)

        self._build_toolbar()

        self.findbar = FindBar(self.find_next, self.find_prev, self._close_findbar, self)
        self.findbar.hint_label.setText("Enter: next  |  Shift+Enter: prev  |  Esc: close")
        self.findbar.edit.textChanged.connect(self._on_search_text_changed)
        self.findbar.setVisible(False)

        reading_area = QWidget()
        row = QHBoxLayout(reading_area)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.outline)
        row.addWidget(self.view, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar)
        layout.addWidget(self.findbar)
        layout.addWidget(reading_area, 1)

        # Ctrl+wheel zooms, the way every reader since the browser does.
        self.view.viewport().installEventFilter(self)

        self._build_shortcuts()

        # Focusing the tab focuses the document, so reading keys (Space,
        # Home / End, PageUp / PageDown) work the moment the tab opens.
        self.setFocusProxy(self.view)

        nav = self.view.pageNavigator()
        nav.currentPageChanged.connect(self._on_page_changed)

        self.update_theme(is_dark)

        # The saved reading position is applied on first show, not here:
        # a page jump computes scroll offsets from the view's geometry,
        # which is still 0x0 until the tab is laid out.
        self._restore_pending = self._load()

    # -- construction ------------------------------------------------------

    def _build_toolbar(self):
        self._toolbar = QWidget()
        self._toolbar.setObjectName("PdfToolbar")
        bar = QHBoxLayout(self._toolbar)
        bar.setContentsMargins(10, 4, 10, 4)
        bar.setSpacing(6)

        self._outline_btn = QToolButton()
        self._outline_btn.setText("Contents")
        self._outline_btn.setToolTip("Table of contents (F12)")
        self._outline_btn.setCheckable(True)
        self._outline_btn.setEnabled(False)
        self._outline_btn.clicked.connect(self.toggle_outline)
        bar.addWidget(self._outline_btn)
        bar.addSpacing(8)

        bar.addWidget(QLabel("Page"))
        self._page_edit = QLineEdit()
        self._page_edit.setFixedWidth(48)
        self._page_edit.setAlignment(Qt.AlignCenter)
        # Click-to-focus only: initial focus and Tab must stay with the
        # document view, or every fresh tab would trap keystrokes here.
        self._page_edit.setFocusPolicy(Qt.ClickFocus)
        self._page_edit.returnPressed.connect(self._on_page_entered)
        bar.addWidget(self._page_edit)
        self._page_total = QLabel("/ 0")
        bar.addWidget(self._page_total)

        bar.addStretch(1)

        self._zoom_out_btn = QToolButton()
        self._zoom_out_btn.setText("−")
        self._zoom_out_btn.setToolTip("Zoom out (Ctrl+-)")
        self._zoom_out_btn.clicked.connect(self.zoom_out)
        bar.addWidget(self._zoom_out_btn)

        self._zoom_label = QLabel("Fit")
        self._zoom_label.setMinimumWidth(42)
        self._zoom_label.setAlignment(Qt.AlignCenter)
        bar.addWidget(self._zoom_label)

        self._zoom_in_btn = QToolButton()
        self._zoom_in_btn.setText("+")
        self._zoom_in_btn.setToolTip("Zoom in (Ctrl++)")
        self._zoom_in_btn.clicked.connect(self.zoom_in)
        bar.addWidget(self._zoom_in_btn)

        bar.addSpacing(8)

        self._fit_width_btn = QToolButton()
        self._fit_width_btn.setText("Fit Width")
        self._fit_width_btn.setToolTip("Fit page width (Ctrl+0)")
        self._fit_width_btn.setCheckable(True)
        self._fit_width_btn.setChecked(True)
        self._fit_width_btn.clicked.connect(lambda: self._set_zoom_mode(QPdfView.ZoomMode.FitToWidth))
        bar.addWidget(self._fit_width_btn)

        self._fit_page_btn = QToolButton()
        self._fit_page_btn.setText("Fit Page")
        self._fit_page_btn.setToolTip("Fit whole page")
        self._fit_page_btn.setCheckable(True)
        self._fit_page_btn.clicked.connect(lambda: self._set_zoom_mode(QPdfView.ZoomMode.FitInView))
        bar.addWidget(self._fit_page_btn)

    def _build_shortcuts(self):
        # Ctrl combos work anywhere inside the tab, including while the
        # find bar's edit has focus.
        for keys, handler in (
            (QKeySequence.ZoomIn, self.zoom_in),
            (QKeySequence("Ctrl+="), self.zoom_in),
            (QKeySequence.ZoomOut, self.zoom_out),
            (QKeySequence("Ctrl+0"), lambda: self._set_zoom_mode(QPdfView.ZoomMode.FitToWidth)),
            (QKeySequence("Ctrl+1"), self.actual_size),
            (QKeySequence("Ctrl+2"), lambda: self._set_zoom_mode(QPdfView.ZoomMode.FitInView)),
            (QKeySequence(Qt.Key_F12), self.toggle_outline),
        ):
            sc = QShortcut(keys, self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(handler)

        # Bare reading keys bind to the view only, so typing a space, a
        # letter, or pressing Home in the find bar's edit still edits
        # text there. j/k/n/p/g mirror SumatraPDF's vim-style bindings.
        for keys, handler in (
            (QKeySequence(Qt.Key_Space), lambda: self._scroll_page(+1)),
            (QKeySequence(Qt.SHIFT | Qt.Key_Space), lambda: self._scroll_page(-1)),
            (QKeySequence(Qt.Key_Home), lambda: self.jump_to_page(0)),
            (QKeySequence(Qt.Key_End), lambda: self.jump_to_page(self.document.pageCount() - 1)),
            (QKeySequence(Qt.Key_J), lambda: self._scroll_lines(+1)),
            (QKeySequence(Qt.Key_K), lambda: self._scroll_lines(-1)),
            (QKeySequence(Qt.Key_N), self.next_page),
            (QKeySequence(Qt.Key_P), self.prev_page),
            (QKeySequence(Qt.Key_Plus), self.zoom_in),
            (QKeySequence(Qt.Key_Minus), self.zoom_out),
            (QKeySequence(Qt.Key_G), self._focus_page_box),
            (QKeySequence.Copy, self.view.copy_selection),
            (QKeySequence(Qt.Key_Escape), self.view.clear_selection),
        ):
            sc = QShortcut(keys, self.view)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(handler)

    # -- loading -----------------------------------------------------------

    def _load(self) -> bool:
        """Load ``_file_path``, prompting for a password when the file
        is protected. Sets ``load_ok`` / ``load_error``."""
        error = self.document.load(self._file_path)
        while error == QPdfDocument.Error.IncorrectPassword:
            password, ok = QInputDialog.getText(
                self, "Password required",
                f"'{os.path.basename(self._file_path)}' is password protected.\n"
                "Enter the password to open it:",
                QLineEdit.Password,
            )
            if not ok:
                self.load_error = "The document is password protected."
                self.load_ok = False
                return False
            self.document.setPassword(password)
            error = self.document.load(self._file_path)

        if error != QPdfDocument.Error.None_:
            messages = {
                QPdfDocument.Error.FileNotFound: "The file could not be found.",
                QPdfDocument.Error.InvalidFileFormat: "The file is not a valid PDF.",
                QPdfDocument.Error.UnsupportedSecurityScheme:
                    "The document uses an unsupported security scheme.",
            }
            self.load_error = messages.get(error, "The document could not be opened.")
            self.load_ok = False
            return False

        self.load_ok = True
        self.load_error = ""
        total = self.document.pageCount()
        self._page_total.setText(f"/ {total}")
        self._page_edit.setValidator(QIntValidator(1, max(total, 1), self._page_edit))
        self._sync_page_edit()
        has_outline = self._bookmarks.rowCount() > 0
        self._outline_btn.setEnabled(has_outline)
        if not has_outline:
            self._outline_btn.setChecked(False)
            self.outline.setVisible(False)
        return True

    def schedule_reload(self) -> None:
        """Debounced reload for file-watcher events: builds often touch
        the file several times in quick succession, and reloading a
        half-written PDF would flash an error page."""
        if self._reload_pending:
            return
        self._reload_pending = True
        QTimer.singleShot(400, self._do_scheduled_reload)

    def _do_scheduled_reload(self):
        self._reload_pending = False
        if os.path.exists(self._file_path):
            self.reload()

    def reload(self) -> None:
        """Reload after an external change, keeping the reading position.

        A vanished or momentarily invalid file (e.g. mid-write during a
        LaTeX build) leaves the last good render on screen; the watcher
        will fire again once the writer finishes.
        """
        page = self.current_page()
        self.document.close()
        self.view.reset_reader_state()
        if self._load():
            self.jump_to_page(min(page, self.document.pageCount() - 1))
            self.page_changed.emit()

    def document_title(self) -> str:
        """Title from PDF metadata, or the file name without extension."""
        title = self.document.metaData(QPdfDocument.MetaDataField.Title)
        return str(title).strip() or os.path.splitext(os.path.basename(self._file_path))[0]

    # -- reading position --------------------------------------------------

    def showEvent(self, event):
        super().showEvent(event)
        if self._restore_pending:
            self._restore_pending = False
            # Deferred one event-loop turn so the first layout pass has
            # assigned the view its real size before we jump.
            QTimer.singleShot(0, self._restore_view_state)

    def _restore_view_state(self):
        state = load_view_state(self._file_path)
        if not state:
            return
        mode = state.get("zoom_mode")
        if mode == "fit-page":
            self._set_zoom_mode(QPdfView.ZoomMode.FitInView)
        elif mode == "custom":
            zoom = state.get("zoom")
            if isinstance(zoom, (int, float)) and _MIN_ZOOM <= zoom <= _MAX_ZOOM:
                self.view.setZoomFactor(zoom)
                self._set_zoom_mode(QPdfView.ZoomMode.Custom)
        page = state.get("page")
        if isinstance(page, int) and 0 < page < self.document.pageCount():
            self.jump_to_page(page)

    def save_view_state(self):
        if not self.load_ok:
            return
        mode = self.view.zoomMode()
        state = {
            "page": self.current_page(),
            "zoom_mode": {QPdfView.ZoomMode.FitToWidth: "fit-width",
                          QPdfView.ZoomMode.FitInView: "fit-page"}.get(mode, "custom"),
        }
        if mode == QPdfView.ZoomMode.Custom:
            state["zoom"] = self.view.zoomFactor()
        save_view_state(self._file_path, state)

    # -- navigation --------------------------------------------------------

    def current_page(self) -> int:
        return self.view.pageNavigator().currentPage()

    def jump_to_page(self, page: int):
        page = max(0, min(page, self.document.pageCount() - 1))
        nav = self.view.pageNavigator()
        nav.jump(page, QPointF(0, 0), nav.currentZoom())

    def page_display(self) -> str:
        """Status-bar text, e.g. "Page 3 / 42"."""
        return f"Page {self.current_page() + 1} / {self.document.pageCount()}"

    def _on_page_changed(self, *_):
        self._sync_page_edit()
        self.page_changed.emit()

    def _sync_page_edit(self):
        # Do not fight the user while they are typing a target page.
        if not self._page_edit.hasFocus():
            self._page_edit.setText(str(self.current_page() + 1))

    def _on_page_entered(self):
        try:
            page = int(self._page_edit.text()) - 1
        except ValueError:
            self._sync_page_edit()
            return
        self.jump_to_page(page)
        self.view.setFocus()

    def next_page(self):
        self.jump_to_page(self.current_page() + 1)

    def prev_page(self):
        self.jump_to_page(self.current_page() - 1)

    def _scroll_page(self, direction: int):
        """Browser-style Space scrolling: most of a viewport per press."""
        vsb = self.view.verticalScrollBar()
        vsb.setValue(vsb.value() + direction * int(vsb.pageStep() * 0.9))

    def _scroll_lines(self, direction: int):
        """Vim-style j/k: a few scroll-wheel lines per press."""
        vsb = self.view.verticalScrollBar()
        vsb.setValue(vsb.value() + direction * vsb.singleStep() * 3)

    def _focus_page_box(self):
        """SumatraPDF's g ("go to page"): put the cursor in the page box."""
        self._page_edit.setFocus(Qt.ShortcutFocusReason)
        self._page_edit.selectAll()

    # -- table of contents -------------------------------------------------

    def toggle_outline(self):
        if not self._outline_btn.isEnabled():
            return
        show = not self.outline.isVisible()
        self.outline.setVisible(show)
        self._outline_btn.setChecked(show)
        if show:
            self.outline.setFocus()
        else:
            self.view.setFocus()

    def _on_outline_activated(self, index):
        page = index.data(int(QPdfBookmarkModel.Role.Page))
        location = index.data(int(QPdfBookmarkModel.Role.Location))
        if isinstance(page, int) and page >= 0:
            nav = self.view.pageNavigator()
            nav.jump(page, location if isinstance(location, QPointF) else QPointF(0, 0),
                     nav.currentZoom())

    # -- zoom --------------------------------------------------------------

    def _set_zoom_mode(self, mode):
        self.view.setZoomMode(mode)
        self._fit_width_btn.setChecked(mode == QPdfView.ZoomMode.FitToWidth)
        self._fit_page_btn.setChecked(mode == QPdfView.ZoomMode.FitInView)
        self._update_zoom_label()

    def zoom_in(self):
        self._apply_custom_zoom(self.view.zoomFactor() * _ZOOM_STEP)

    def zoom_out(self):
        self._apply_custom_zoom(self.view.zoomFactor() / _ZOOM_STEP)

    def actual_size(self):
        self._apply_custom_zoom(1.0)

    def eventFilter(self, obj, event):
        if (obj is self.view.viewport()
                and event.type() == QEvent.Type.Wheel
                and event.modifiers() & Qt.ControlModifier):
            if event.angleDelta().y() > 0:
                self.zoom_in()
            elif event.angleDelta().y() < 0:
                self.zoom_out()
            return True
        return super().eventFilter(obj, event)

    def _apply_custom_zoom(self, factor: float):
        # Changing the zoom rescales the document but QPdfView keeps the
        # raw pixel scroll offset, silently drifting the visible page.
        # Preserve the relative position instead, so what was being read
        # stays on screen through the zoom change.
        vsb = self.view.verticalScrollBar()
        ratio = vsb.value() / vsb.maximum() if vsb.maximum() else 0.0
        self.view.setZoomFactor(max(_MIN_ZOOM, min(factor, _MAX_ZOOM)))
        self._set_zoom_mode(QPdfView.ZoomMode.Custom)
        if vsb.maximum():
            vsb.setValue(round(ratio * vsb.maximum()))

    def _update_zoom_label(self):
        if self.view.zoomMode() == QPdfView.ZoomMode.Custom:
            self._zoom_label.setText(f"{round(self.view.zoomFactor() * 100)}%")
        else:
            self._zoom_label.setText("Fit")

    # -- find --------------------------------------------------------------

    def toggle_findbar(self):
        if self.findbar.isVisible():
            self._close_findbar()
        else:
            self.findbar.setVisible(True)
            self.findbar.focusIn()

    def _close_findbar(self):
        self.findbar.setVisible(False)
        self._search.setSearchString("")
        self._current_result = -1
        self.findbar.set_match_info("")
        self.view.setFocus()

    def _on_search_text_changed(self):
        self._search.setSearchString(self.findbar.text())
        self._current_result = -1
        self._update_match_label()

    def _start_index(self) -> int:
        """First result on or after the page currently being read, so a
        fresh search begins where the reader is, not at page one."""
        page = self.current_page()
        count = self._search.count()
        for i in range(count):
            if self._search.resultAtIndex(i).page() >= page:
                return i
        return 0

    def find_next(self):
        count = self._search.count()
        if count == 0:
            return
        index = self._start_index() if self._current_result < 0 else self._current_result + 1
        self._jump_to_result(index % count)

    def find_prev(self):
        count = self._search.count()
        if count == 0:
            return
        index = self._start_index() - 1 if self._current_result < 0 else self._current_result - 1
        self._jump_to_result(index % count)

    def _jump_to_result(self, index: int):
        self._current_result = index
        self.view.setCurrentSearchResultIndex(index)
        link = self._search.resultAtIndex(index)
        if link.isValid():
            self.view.pageNavigator().jump(link)
        self._update_match_label()

    def _update_match_label(self):
        if not self.findbar.text():
            self.findbar.set_match_info("")
            return
        count = self._search.count()
        if count == 0:
            self.findbar.set_match_info("No matches")
        elif self._current_result < 0:
            self.findbar.set_match_info(f"{count} matches")
        else:
            self.findbar.set_match_info(f"{self._current_result + 1} / {count}")

    # -- theme -------------------------------------------------------------

    def update_theme(self, is_dark: bool):
        self.is_dark = is_dark
        self._toolbar.setStyleSheet(_TOOLBAR_DARK_CSS if is_dark else _TOOLBAR_LIGHT_CSS)
        self.outline.setStyleSheet(_OUTLINE_DARK_CSS if is_dark else _OUTLINE_LIGHT_CSS)
        self.findbar.is_dark = is_dark
        self.findbar._update_theme()
        # QPdfView paints the surface around pages with the palette's
        # Dark brush; pages themselves always render as authored.
        palette = self.view.palette()
        palette.setBrush(QPalette.Dark, _VIEW_BG_DARK if is_dark else _VIEW_BG_LIGHT)
        self.view.setPalette(palette)
