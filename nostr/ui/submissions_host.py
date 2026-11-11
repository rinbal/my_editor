# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the window does with the submissions of the account in use
(plan 10.4): the Einreichungen view follows the account, the pill beside
the tab says the article's status, a notice at the article says what is
new, and the detail's actions reach the editor.

:class:`SubmissionsHost` keeps this out of the main window, which only
hands it the few things a window has (:class:`Ports`: the tabs, the
panel, the sheets). The detail's primary actions are one table
(:meth:`SubmissionsHost.actions`), keyed by
:class:`~nostr.standup.following.Action`: revising in the editor,
handing in the next version, sending an unsent one again. Publishing
after acceptance, confirming and taking a correction are a later
phase's: each adds its handler to the table and nothing else.

Everything is the account's: when it is let go (sign-out, another
account), the view empties, the pill and the notices go, the compare
forgets its texts and a cover still being fetched is dropped (criterion
19).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Dict, Optional

import shiboken6
from PySide6.QtCore import QObject
from PySide6.QtGui import QColor, QPixmap

from i18n import N_, _

from ..standup import following as rules
from ..standup import formats
from ..standup import thread as threads
from . import appearance
from .publish_article_dialog import ArticleCard
from .standup_compare import CompareDialog, Comparer
from .standup_publications import publication_logo
from .standup_revise_sheet import ReviseSheet, comparable
from .standup_uploads import thumbnail
from .submission_cover import CoverFetch, monogram, png_bytes
from .submission_detail import NOT_OPENED
from .submission_pill import SubmissionPill
from .submissions_view import PANEL_VIEW, SubmissionsView

NOTICE_KEY = "submission"
# A new message, by who sent it (thread.role_of): only a member of the
# Redaktion speaks for the publication.
MESSAGE_FROM_PUBLICATION = N_("New message from {publication} about “{title}”.")
MESSAGE_FROM_PERSON = N_("New message from {name} about “{title}”.")
MESSAGE_FROM_CONTRIBUTOR = N_("New message from a contributor about “{title}”.")
MESSAGE_FROM_SOMEONE = N_("New message about “{title}”.")
NEWS_WORDS = {
    "request_changes": N_("{publication} asks for changes to “{title}”."),
    "accept": N_("{publication} accepted “{title}”."),
    "decline": N_("{publication} declined “{title}”."),
    "published": N_("“{title}” is published in {publication}."),
    "correction": N_("{publication} suggests a correction to “{title}”."),
}
OPEN_IT_FIRST = N_("Open the article in the editor first.")
VERSION_NOT_LOADED = N_("The version you handed in couldn’t be loaded: try again in a moment.")


@dataclass(frozen=True)
class Ports:
    """What the window lends (each a callable, so the host holds no
    window state).

    - ``standup()``: the StandupWriter; ``profile()``: the account in use.
    - ``current_editor()``; ``editor_of(d)``: the tab that holds the
      article ``d``, or None; ``identifier(ed)``: the article's ``d`` in
      a tab (``""`` for no article); ``text_of(ed)``: its Markdown.
    - ``focus(ed)``: bring its tab forward.
    - ``open_text(title, content, d, summary, tags)``: a new tab with a
      version's text (``d`` is ``""`` for a copy to read).
    - ``submission_of(ed, profile)``: ``(draft, cover, byline, private)``
      of the article in a tab, as Submit to a Publication builds them.
    - ``show_view(key)``: the Drafts panel, showing that view.
    - ``submit()``: Submit to a Publication for the current tab.
    - ``notice_of(ed)``: the tab's DocumentNotice.
    - ``say(text)``: the status bar. ``keep(sheet)``: a sheet follows
      the appearance while it is open.
    - ``avatar_of(pubkey)``, ``request_logo(pubkey, url)``, ``is_dark()``,
      ``window()``.
    """

    standup: Callable
    profile: Callable
    current_editor: Callable
    editor_of: Callable
    identifier: Callable
    text_of: Callable
    focus: Callable
    open_text: Callable
    submission_of: Callable
    show_view: Callable
    submit: Callable
    notice_of: Callable
    say: Callable
    keep: Callable
    avatar_of: Callable
    request_logo: Callable
    is_dark: Callable
    window: Callable


class SubmissionsHost(QObject):
    """See the module docstring."""

    def __init__(self, ports: Ports, *, view: SubmissionsView, pill: SubmissionPill,
                 name_of: Callable[[str], str], comparer: Optional[Comparer] = None,
                 cover_fetch: Callable[[QObject], CoverFetch] = CoverFetch,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._ports, self.view, self.pill, self._name_of = ports, view, pill, name_of
        self.comparer = comparer or Comparer(self)
        self._cover_fetch = cover_fetch             # a seam: the fetch of a sheet's cover
        self._following = None
        self._writer = None
        self._noticed: Dict[object, tuple] = {}     # an editor -> (sid, the notice's text)
        # The sheets that hold a version's text: closed, and so deleted,
        # with the account (each deletes itself once it is closed).
        self._dialog: Optional[CompareDialog] = None
        self._sheet: Optional[ReviseSheet] = None
        view.set_handled(self.actions())
        view.set_edited(self.edited)
        view.action_requested.connect(self._on_action)
        view.open_in_editor.connect(self.open_in_editor)
        view.compare_requested.connect(self.compare)
        view.submit_requested.connect(lambda: ports.submit())
        pill.clicked.connect(lambda: self.open_detail(pill.sid))

    # -- the account's scope -----------------------------------------------------

    def follow(self) -> None:
        """The account in use is followed (at launch, and whenever it
        changes): the list read and the lookup started, when a server to
        submit to is set; without one nothing is read or asked."""
        ports = self._ports
        profile, writer = ports.profile(), ports.standup()
        if writer is not self._writer:
            self._writer = writer
            writer.stopped.connect(self.account_stopped)
        following = None
        if profile is not None and writer.server:
            following = writer.following(profile)
            if following is not self._following:
                following.changed.connect(self.update_pill)
                following.news.connect(self._on_news)
            following.start()
        self._following = following
        self.view.set_following(following, signed_in=profile is not None,
                                server=bool(writer.server))
        self.update_pill()

    def account_stopped(self, *_writer) -> None:
        """The account was let go: nothing of it stays on screen, and
        nothing of it still on its way is heard."""
        self._following = None
        self.view.set_following(None, signed_in=False)
        self.pill.clear()
        self.comparer.stop()
        for sheet in (self._dialog, self._sheet):
            if sheet is not None and shiboken6.isValid(sheet):
                sheet.reject()
        self._dialog = self._sheet = None
        noticed, self._noticed = self._noticed, {}
        for ed, (_sid, text) in noticed.items():
            notice = self._ports.notice_of(ed) if shiboken6.isValid(ed) else None
            if notice is not None and notice.text() == text:
                notice.hide()

    # -- the pill and the notice ---------------------------------------------------

    def _logo(self, address: str, size: int) -> QPixmap:
        name = self._following.publication_name(address)
        try:
            pubkey = formats.parse_address(address)[0]
        except formats.FormatError:
            pubkey = ""
        picture = self._following.publication_picture(address)
        if pubkey and picture and picture.startswith("https://"):
            self._ports.request_logo(pubkey, picture)
        return publication_logo(name, pubkey, self._ports.avatar_of(pubkey) if pubkey else None,
                                size)

    def update_pill(self) -> None:
        """The pill says the status of the article in the current tab,
        while the account in use submitted it."""
        ed = self._ports.current_editor()
        d = self._ports.identifier(ed) if ed is not None else ""
        row = self._following.row_for_work(d) if self._following is not None and d else None
        if row is None:
            self.pill.clear()
            return
        self.pill.show_status(row.sid, rules.pill_word(row, self._name_of),
                              self._following.publication_name(row.publication),
                              self._logo(row.publication, 18), turn=row.turn)

    def _on_news(self, news) -> None:
        """A notice at the article for a new message or a result (plan
        7.4), unless its place is taken by another notice: the pill and
        the list say it all the same."""
        ed = self._ports.editor_of(news.d) if news.d else None
        notice = self._ports.notice_of(ed) if ed is not None else None
        if notice is None or not notice.isHidden():
            return
        text = self._news_words(news)
        self._noticed[ed] = (news.sid, text)
        notice.present(text, actions=[(NOTICE_KEY, _("View"))])

    def _news_words(self, news) -> str:
        if news.kind != "message":
            return _(NEWS_WORDS[news.kind]).format(publication=news.publication,
                                                   title=news.title)
        name = self._name_of(news.sender)
        if news.role is threads.Role.MEMBER:
            words = MESSAGE_FROM_PUBLICATION
        elif name:
            words = MESSAGE_FROM_PERSON
        elif news.role is threads.Role.PARTICIPANT:
            words = MESSAGE_FROM_CONTRIBUTOR
        else:
            words = MESSAGE_FROM_SOMEONE
        return _(words).format(publication=news.publication, name=name, title=news.title)

    def notice_acted(self, ed, key: str) -> bool:
        """Ansehen on a notice of this host: the detail opens."""
        if key != NOTICE_KEY or ed not in self._noticed:
            return False
        self.open_detail(self._noticed.pop(ed)[0])
        return True

    def open_detail(self, sid: str) -> None:
        if sid and self._following is not None:
            self._ports.show_view(PANEL_VIEW)
            self.view.show_detail(sid)

    # -- the detail's actions ------------------------------------------------------

    def actions(self) -> Dict[rules.Action, Callable[[str], None]]:
        """The primary actions this window handles, by Action: the one
        table a later phase adds publishing, confirming and the
        correction to."""
        return {rules.Action.REVISE: lambda sid: self.open_in_editor(sid, 0),
                rules.Action.HAND_IN: self.hand_in,
                rules.Action.RESEND: self.send_again}

    def _on_action(self, sid: str, action) -> None:
        handler = self.actions().get(action)
        if handler is not None:
            handler(sid)

    def _row(self, sid: str):
        return self._following.row(sid) if self._following is not None else None

    def edited(self, row) -> bool:
        """Whether the text in the editor differs from the version handed
        in (so the next one can be handed in)."""
        if self._following is None or not row.open or not row.d:
            return False
        ed = self._ports.editor_of(row.d)
        version = self._following.reading.latest(row.sid)
        if ed is None or version is None or version.revision != row.view.revision:
            return False
        return comparable(self._ports.text_of(ed)) != comparable(
            str(version.work.get("content") or ""))

    def open_in_editor(self, sid: str, revision: int) -> None:
        """Im Editor öffnen: the tab that holds the article comes forward;
        a version (``revision``, or the latest when no tab holds the
        article) opens in a new tab, from its verified envelope only."""
        row, following, ports = self._row(sid), self._following, self._ports
        if row is None:
            return
        ed = ports.editor_of(row.d) if row.d else None
        latest = row.view.revision if row.view is not None else 1
        if ed is not None and revision in (0, latest):
            ports.focus(ed)
            return
        number = revision or latest

        def opened(version) -> None:
            if self._following is not following:
                return
            if version is None:
                ports.say(_(NOT_OPENED))
                return
            work, working = version.work, ed is None and number == latest
            title = str(work.get("title") or row.title)
            ports.open_text(
                title if working else _("{title} (version {number})").format(
                    title=title, number=number),
                str(work.get("content") or ""), str(work.get("d") or row.d) if working else "",
                str(work.get("summary") or ""), tuple(work.get("tags") or ()))

        following.version(sid, number, opened)

    def send_again(self, sid: str) -> None:
        """Erneut senden for an entry the server has nothing of: Submit
        to a Publication for its text, which reuses the entry (13)."""
        row = self._row(sid)
        ed = self._ports.editor_of(row.d) if row is not None and row.d else None
        if ed is None:
            self._ports.say(_(OPEN_IT_FIRST))
            return
        self._ports.focus(ed)
        self._ports.submit()

    def hand_in(self, sid: str) -> None:
        """Neue Fassung abgeben …: the sheet, for the article's tab and
        the version handed in before (read first when it is not here)."""
        row, following, ports = self._row(sid), self._following, self._ports
        ed = ports.editor_of(row.d) if row is not None and row.d else None
        if row is None or not row.open:
            return
        if ed is None:
            ports.say(_(OPEN_IT_FIRST))
            return

        def with_version(previous) -> None:
            if self._following is not following or not shiboken6.isValid(ed):
                return
            if previous is None:
                ports.say(_(VERSION_NOT_LOADED))
                return
            profile = ports.profile()
            made = ports.submission_of(ed, profile)
            if made is None:
                return
            draft, cover, byline, private = made
            # The sheet needs the submission, not its secrets: the run
            # takes them from the list.
            sheet = self._sheet = ReviseSheet(
                writer=ports.standup(), profile=profile, draft=replace(draft, d=row.d),
                cover=cover, byline=byline, row=replace(row, entry=None),
                publication=following.publication_name(row.publication),
                logo=self._logo(row.publication, 40), previous=previous,
                comparer=self.comparer, private_names=private, is_dark=ports.is_dark(),
                parent=ports.window())
            sheet.view_submission.connect(self.open_detail)
            ports.keep(sheet)
            sheet.open()

        following.version(sid, row.view.revision, with_version)

    def compare(self, sid: str, revision: int) -> None:
        """Änderungen zeigen on a version's card: that version beside the
        one before it, word by word."""
        following, ports = self._following, self._ports
        if following is None or revision < 2:
            return

        def with_both(old, new) -> None:
            if self._following is not following:
                return
            if old is None or new is None:
                ports.say(_(NOT_OPENED))
                return
            self._dialog = CompareDialog(
                comparer=self.comparer,
                old=comparable(str(old.work.get("content") or "")),
                new=comparable(str(new.work.get("content") or "")),
                heading=_("Version {before} and version {after}").format(
                    before=revision - 1, after=revision),
                is_dark=ports.is_dark(), parent=ports.window())
            ports.keep(self._dialog)
            self._dialog.open()

        following.version(sid, revision - 1, lambda old: following.version(
            sid, revision, lambda new: with_both(old, new)))

    # -- a cover that is only a web address (handed over from M-2) ---------------------

    def cover_for_sheet(self, sheet, url: str, title: str) -> None:
        """The submit sheet's card and the revision's small cover for a
        cover given only as a web address: a monogram at once, the picture
        when it comes (submission_cover.CoverFetch); when it does not,
        nothing is said."""
        r = appearance.roles(self._ports.is_dark())
        sheet.set_cover(monogram(title, ArticleCard.WIDTH - 2, ArticleCard.COVER_HEIGHT,
                                 QColor(r.soft), QColor(r.icon)), None)
        fetch = self._cover_fetch(sheet)
        sheet.finished.connect(lambda _result: fetch.stop())

        def fetched(image) -> None:
            if image is not None and shiboken6.isValid(sheet):
                sheet.set_cover(QPixmap.fromImage(image), thumbnail(png_bytes(image)))

        fetch.fetch(url, fetched)

    def apply_theme(self, is_dark: bool) -> None:
        self.view.apply_theme(is_dark)
        self.pill.apply_theme(is_dark)
        self.update_pill()


__all__ = ["NEWS_WORDS", "NOTICE_KEY", "Ports", "SubmissionsHost"]
