# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which tabs show their article's head on the page, and keeping each in
step with the article.

The window owns the article (its details, kept with the tab, and the
title it publishes with while none is set) and decides which tabs are
articles; this module puts an :class:`~nostr.ui.article_header.ArticleHeader`
in the header slot of each such tab's editor (editor_header.EditorHeader),
shows the article in it as it changes, and hands back what is typed there
or asked of the cover. The window calls :meth:`ArticleHeaders.update`
whenever a tab's article, or whether it is one, may have changed.
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PySide6.QtCore import QObject

from editor_header import EditorHeader

from ..article_details import ArticleDetails
from ..blossom.store import MediaStore
from .article_header import ArticleHeader


class ArticleHeaders(QObject):
    """The article heads of the window's tabs (see the module).

    ``wanted(ed)``: whether ``ed`` shows its article's head now.
    ``details_of(ed)`` / ``set_details(ed, details)``: the tab's article.
    ``article_now(ed)``: ``(title, verdict)``, the title it publishes with
    while none is set and the Publication Check's verdict (or None), from
    one reading of its text.
    ``choose_cover(ed)``: open the cover picker for ``ed``'s article.
    ``show_readiness(ed)``: show the check in full (Article Details).
    """

    def __init__(self, *, wanted: Callable[[object], bool],
                 details_of: Callable[[object], ArticleDetails],
                 set_details: Callable[[object, ArticleDetails], None],
                 article_now: Callable[[object], Tuple[str, object]],
                 choose_cover: Callable[[object], None],
                 show_readiness: Callable[[object], None] = lambda ed: None,
                 media_store: Optional[MediaStore] = None, is_dark: bool = False,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._wanted = wanted
        self._details_of = details_of
        self._set_details = set_details
        self._article_now = article_now
        self._choose_cover = choose_cover
        self._show_readiness = show_readiness
        self._media_store = media_store
        self._is_dark = is_dark

    @staticmethod
    def header_of(ed) -> Optional[ArticleHeader]:
        slot: Optional[EditorHeader] = getattr(ed, "_article_header", None)
        return slot.header if slot is not None else None

    def update(self, ed, now: Optional[Tuple[str, object]] = None) -> None:
        """Show ``ed``'s article on its page, or take the head away when it
        is no article now. ``now``: its article_now, when the caller has it."""
        if ed is None:
            return
        slot: Optional[EditorHeader] = getattr(ed, "_article_header", None)
        if not self._wanted(ed):
            if slot is not None:
                slot.detach()
                ed._article_header = None
            return
        if slot is None:
            slot = ed._article_header = EditorHeader(ed, self._make(ed))
        header = slot.header
        header.set_text_font(ed.font())
        details = self._details_of(ed)
        header.show_article(details.title, details.summary, details.image)
        header.set_compact(getattr(ed, "_language_link", None) is not None)
        self.show_article_now(ed, now or self._article_now(ed))

    def show_article_now(self, ed, now: Tuple[str, object]) -> None:
        """The text changed: the title ``ed``'s article publishes with and
        the check's verdict on it, again."""
        header = self.header_of(ed)
        if header is not None:
            title, result = now
            header.set_derived_title(title)
            header.show_readiness(result)

    def set_dark(self, is_dark: bool, editors) -> None:
        self._is_dark = is_dark
        for ed in editors:
            header = self.header_of(ed)
            if header is not None:
                header.set_dark(is_dark)

    def _make(self, ed) -> ArticleHeader:
        header = ArticleHeader(media_store=self._media_store, is_dark=self._is_dark)
        header.edited.connect(lambda title, summary, e=ed: self._set_details(
            e, self._details_of(e).with_changes(title=title, summary=summary)))
        header.cover_removed.connect(lambda e=ed: self._set_details(
            e, self._details_of(e).with_changes(image="")))
        header.cover_requested.connect(lambda e=ed: self._choose_cover(e))
        header.readiness_requested.connect(lambda e=ed: self._show_readiness(e))
        return header
