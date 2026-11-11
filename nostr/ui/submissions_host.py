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

The pill and the detail follow the article in front (RG-M3-b 1): the
window says :meth:`SubmissionsHost.article_changed` as it is typed in
(drawn again :data:`FOLLOW_MS` after the last change) and when another
tab comes forward or its details change (at once).

Everything is the account's: when it is let go (sign-out, another
account), the view empties, the pill and the notices go, the compare
forgets its texts and a cover still being fetched is dropped (criterion
19).
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, replace
from typing import Callable, Dict, Optional

import shiboken6
from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPixmap

from i18n import N_, _

from .. import web_readers
from ..bech32 import decode_naddr, encode_naddr
from ..standup import following as rules
from ..standup import formats, publishing
from ..standup import thread as threads
from . import appearance
from .publish_article_dialog import ArticleCard
from .standup_compare import CompareDialog, Comparer
from .standup_publications import publication_logo
from ..standup.errors import Code, cause_words
from .standup_confirm_sheet import ConfirmSheet
from .standup_correction_sheet import CorrectionSheet
from .standup_publish import CorrectionPublish, Stage
from .standup_publish_sheet import PublishSheet
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
FOLLOW_MS = 400
OPEN_IT_FIRST = N_("Open the article in the editor first.")
NOT_FOUND = N_("MyEditor can’t find this article. Open it in the editor, then send it again.")
VERSION_NOT_LOADED = N_("The version you handed in couldn’t be loaded: try again in a moment.")
ACCEPTED_NOT_LOADED = N_("The accepted version couldn’t be loaded yet: try again in a moment.")
PUBLISHED_NOT_LOADED = N_("The published article couldn’t be found yet: try again in a moment.")
# The web's words for the same moments (RG-M4-b 13).
REMINDED = N_("{name} was reminded.")
REMINDED_TODAY = N_("{name} was already reminded today.")
NOT_REMINDED = N_("{name} wasn’t reminded.")
# Erneut senden for a public work's report (RG-M4-c 3), when it failed.
NOT_LEARNED = N_("{publication} still hasn’t learned of it.")


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
    - ``open_article(d)``: the tab that holds the article ``d``, opened
      from where this computer keeps it when no tab does, or None.
    - ``zap_address(pubkey, answer)``: the lightning address a profile
      names (``""`` for none, None when it couldn't be told);
      ``edit_profile()``: Edit Profile for the account in use.
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
    open_article: Callable = lambda d: None
    zap_address: Callable = lambda pubkey, answer: answer(None)
    edit_profile: Callable = lambda: None


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
        self._publish_sheet: Optional[PublishSheet] = None
        self._confirm_sheet: Optional[ConfirmSheet] = None
        self._correction_sheet: Optional[CorrectionSheet] = None
        self._heard: "weakref.WeakSet" = weakref.WeakSet()     # publish runs listened to
        self._typing = QTimer(self)
        self._typing.setSingleShot(True)
        self._typing.setInterval(FOLLOW_MS)
        self._typing.timeout.connect(self._follow_article)
        view.set_handled(self.actions())
        view.set_edited(self.edited)
        view.action_requested.connect(self._on_action)
        view.open_in_editor.connect(self.open_in_editor)
        view.compare_requested.connect(self.compare)
        view.remind_requested.connect(self.remind)
        view.submit_requested.connect(lambda: ports.submit())
        pill.clicked.connect(lambda: self.open_detail(pill.sid))

    # -- the account's scope -----------------------------------------------------

    def follow(self) -> None:
        """The account in use is followed (at launch, and whenever it
        changes), when a server to submit to is set; without one nothing
        is read or asked. The list and the read state are opened by the
        account's signer: with the key on this computer they are read
        now; with a signer app, once the person opens Einreichungen (or
        the detail, through the pill or a sheet), so that starting
        MyEditor never asks the signer app (RG-M3-a 12)."""
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
            if getattr(profile, "is_local", False):
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
        for sheet in (self._dialog, self._sheet, self._publish_sheet, self._confirm_sheet,
                      self._correction_sheet):
            if sheet is not None and shiboken6.isValid(sheet):
                sheet.reject()
        self._dialog = self._sheet = self._publish_sheet = self._confirm_sheet = None
        self._correction_sheet = None
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

    def article_changed(self, *, typing: bool = False) -> None:
        """The article in front changed: its text (``typing``: once the
        typing pauses), its tab or its details (at once)."""
        if typing:
            self._typing.start()
        else:
            self._typing.stop()
            self._follow_article()

    def _follow_article(self) -> None:
        """The pill, and the detail when it shows the article in front:
        Neue Fassung abgeben appears once the text differs, and goes when
        it is the same again."""
        self.update_pill()
        if self._following is not None and self.view.showing_detail():
            self.view.detail.refresh()

    def update_pill(self) -> None:
        """The pill says the status of the article in the current tab,
        while the account in use submitted it; a finished one only until
        what it ended with was read (RG-M3-b 13)."""
        ed = self._ports.current_editor()
        d = self._ports.identifier(ed) if ed is not None else ""
        row = self._following.row_for_work(d) if self._following is not None and d else None
        if row is None or row.section is rules.Section.DONE and not row.unread:
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
                rules.Action.RESEND: self.send_again,
                rules.Action.PUBLISH: self.publish,
                rules.Action.VIEW: self.view_published,
                rules.Action.CONFIRM: self.confirm,
                rules.Action.REMIND: self.remind,
                rules.Action.REPORT: self.report_again,
                rules.Action.ASK: lambda sid: self.ask(sid, ""),
                rules.Action.COMPARE_CORRECTION: self.compare_correction}

    def _on_action(self, sid: str, action) -> None:
        handler = self.actions().get(action)
        if handler is not None:
            handler(sid)

    def _row(self, sid: str):
        return self._following.row(sid) if self._following is not None else None

    def _people(self, run) -> list:
        """Everyone the article credits (the writer first when others
        wrote it too), by name."""
        return [self._name_of(pk) or _("Someone")
                for pk in publishing.credited(run.revision, run.writer)]

    def names_learned(self) -> None:
        """A person's name came in from their profile: the list, the
        detail and an open sheet say it, with nothing read again."""
        self.view.names_learned()
        publish, confirm = self._publish_sheet, self._confirm_sheet
        if publish is not None and shiboken6.isValid(publish):
            publish.show_people(self._people(publish.run))
        if confirm is not None and shiboken6.isValid(confirm):
            confirm.show_names()

    def _accepted_section(self, sid: str) -> str:
        """The section the verified accept named (its signed body), or ""."""
        accepted = self._following.reading.accepted(sid) if self._following is not None else None
        return accepted.section if accepted is not None else ""

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
        """Erneut senden (RG-M3-a 6, b 3). The article is opened by its
        link name when no tab holds it; when it cannot be found, one line
        says so in place. An entry the server has nothing of goes through
        Submit to a Publication, which reuses the entry (13); a version
        stored but not shown to be the writer's ("Nicht eingereicht",
        12.4) goes again as the next version."""
        row = self._row(sid)
        if row is None:
            return
        ed = (self._ports.editor_of(row.d) or self._ports.open_article(row.d)) if row.d else None
        if ed is None:
            self._say_in_place(sid, _(NOT_FOUND))
            return
        if row.situation is rules.Situation.NOT_HANDED_IN:
            self.hand_in(sid, again=True)
            return
        self._ports.focus(ed)
        self._ports.submit()

    def _say_in_place(self, sid: str, text: str) -> None:
        """Said where it was asked for: on the detail when it shows ``sid``."""
        detail = self.view.detail
        if self.view.showing_detail() and detail.sid == sid:
            detail.say(text)
        else:
            self._ports.say(text)

    def hand_in(self, sid: str, *, again: bool = False) -> None:
        """Neue Fassung abgeben …: the sheet, for the article's tab and
        the version handed in before (read first when it is not here).
        ``again``: the text goes once more as the next version, beside
        the writer's newest version that can still be opened, if any."""
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
            if previous is None and not again:
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
                again=again, parent=ports.window())
            sheet.view_submission.connect(self.open_detail)
            ports.keep(sheet)
            sheet.open()

        # Sent again, the version stored last is not the writer's: the one
        # before it is, when there is one.
        number = row.view.revision - 1 if again else row.view.revision
        if number < 1:
            with_version(None)
        else:
            following.version(sid, number, with_version)

    def publish(self, sid: str) -> None:
        """Veröffentlichen … (M13): the sheet of the account's one publish
        run of ``sid``, on the verified accept of the verified revision
        opened here; it says when the editor's text differs from it."""
        row, ports = self._row(sid), self._ports
        if row is None or row.view is None:
            return
        profile = ports.profile()
        run = ports.standup().publisher(profile, sid)
        if run is None:
            self._say_in_place(sid, _(ACCEPTED_NOT_LOADED))
            return
        if isinstance(run, CorrectionPublish):
            # A correction taken and not public yet (RG-M4-b 8): its own
            # sheet, which publishes it without confirming it again.
            self.compare_correction(sid)
            return
        work = run.revision.get("work") or {}
        ed = ports.editor_of(row.d) if row.d else None
        differs = ed is not None and comparable(ports.text_of(ed)) != comparable(
            str(work.get("content") or ""))
        people = self._people(run)
        sheet = self._publish_sheet = PublishSheet(
            run=run, publication=self._following.publication_name(row.publication),
            number=row.view.revision, section=self._accepted_section(sid),
            account_name=self._name_of(run.writer), people=people, differs=differs,
            is_dark=ports.is_dark(), parent=ports.window())
        sheet.view_requested.connect(self._view_naddr)
        sheet.ask_requested.connect(lambda words: self.ask(sid, words))
        self._hear(run)
        ports.keep(sheet)
        sheet.open()

    def _publish_said(self, run) -> None:
        """With its sheet closed, what stopped a run is said once in the
        detail; a change meanwhile closes the sheet and is said there too."""
        open_here = any(sheet is not None and shiboken6.isValid(sheet) and sheet.isVisible()
                        and sheet.run is run
                        for sheet in (self._publish_sheet, self._correction_sheet))
        if run.stage is Stage.CHANGED or not open_here and run.stage in (
                Stage.FAILED, Stage.NOT_STORED, Stage.SOMEONE_ELSES, Stage.NOT_CHECKED,
                Stage.TOO_LONG):
            self._say_in_place(run.sid, run.words)

    def _hear(self, run) -> None:
        if run not in self._heard:
            # With the sheet closed, a run that could not finish says so
            # once in the detail (lessons 36); it goes on otherwise.
            self._heard.add(run)
            run.changed.connect(lambda: self._publish_said(run))

    def confirm(self, sid: str) -> None:
        """Bestätigen … (M14): the split the verified accept names, for the
        verified revision it accepted."""
        row, following, ports = self._row(sid), self._following, self._ports
        if row is None or row.view is None:
            return
        reading = following.reading
        accepted, version = reading.accepted(sid), reading.latest(sid)
        shares = reading.accepted_split(sid)
        if (accepted is None or version is None or accepted.revision != row.view.revision
                or version.revision_hash != row.view.revision_hash
                or (accepted.split_rumor_id and shares is None)):
            self._say_in_place(sid, _(ACCEPTED_NOT_LOADED))
            return
        revision = version.opened.revision if version.opened is not None else {}
        sheet = self._confirm_sheet = ConfirmSheet(
            role=row.role, publication=following.publication_name(row.publication),
            number=row.view.revision, shares=shares, me=following.me, name_of=self._name_of,
            zap_address=ports.zap_address, **publishing.rights_of(revision, following.me),
            confirm=lambda confirms, done: following.confirm(
                sid, confirms=confirms, revision_hash=version.revision_hash,
                split_rumor_id=accepted.split_rumor_id, on_done=done),
            is_dark=ports.is_dark(), parent=ports.window())
        sheet.ask_requested.connect(lambda words: self.ask(sid, words))
        sheet.edit_profile_requested.connect(ports.edit_profile)
        ports.keep(sheet)
        sheet.open()

    def compare_correction(self, sid: str) -> None:
        """Vergleichen … (M15): the correction beside the article as it
        stands in public (never an earlier correction: RG-M4-a 6), what
        else it changes in plain words, and Korrektur übernehmen, bound to
        the correction's verified revision."""
        row, following, ports = self._row(sid), self._following, self._ports
        if row is None or row.view is None:
            return
        run = ports.standup().publisher(ports.profile(), sid)
        published = getattr(run, "published_event", None)
        if run is None or published is None:
            self._say_in_place(sid, _(NOT_OPENED))
            return
        old, new = publishing.correction_texts(run.revision, published)
        sheet = self._correction_sheet = CorrectionSheet(
            run=run, publication=following.publication_name(row.publication), old=old, new=new,
            note=str(run.revision.get("change_summary") or ""), comparer=self.comparer,
            changes=publishing.correction_changes(run.revision, published),
            is_dark=ports.is_dark(), parent=ports.window())
        sheet.ask_requested.connect(lambda words: self.ask(sid, words))
        self._hear(run)
        ports.keep(sheet)
        sheet.open()

    def ask(self, sid: str, words: str) -> None:
        """Rückfrage stellen: the detail of ``sid``, its composer holding
        the question's first words and the focus."""
        self.open_detail(sid)
        detail = self.view.detail
        if detail.sid == sid:
            detail.composer.setPlainText(words)
            detail.composer.moveCursor(detail.composer.textCursor().MoveOperation.End)
            detail.composer.setFocus()

    def remind(self, sid: str, to: str = "") -> None:
        """Paul erinnern (12.4 ``/remind``): the one person of the button
        (RG-M4-b 11; without one, the first still to confirm), said in
        place in the web's words; then their button goes."""
        row, following = self._row(sid), self._following
        waiting = rules.waiting_for(row) if row is not None else ()
        to = to or next((pk for pk in waiting if not following.reminded(sid, pk)), "")
        if to not in waiting:
            return
        name = self._name_of(to) or _("Someone")

        def done(error) -> None:
            if error is None:
                self._say_in_place(sid, _(REMINDED).format(name=name))
            elif error.code == Code.RATE_LIMITED:
                self._say_in_place(sid, _(REMINDED_TODAY).format(name=name))
            else:
                cause = cause_words(error.code)
                self._say_in_place(sid, f"{_(NOT_REMINDED).format(name=name)} {_(cause)}")

        following.remind(sid, to, done)

    def report_again(self, sid: str) -> None:
        """Erneut senden: a public work's report, once no quiet try is left
        (RG-M4-c 3); a failure is said in place, with its cause."""
        row, following = self._row(sid), self._following
        if row is None or following is None:
            return
        publication = following.publication_name(row.publication)

        def done(error) -> None:
            if error is not None:
                cause = cause_words(getattr(error, "code", ""))
                self._say_in_place(sid, f"{_(NOT_LEARNED).format(publication=publication)} "
                                        f"{_(cause)}")

        following.unlisted.send_again(sid, done)

    def view_published(self, sid: str) -> None:
        """Ansehen: the published article, where articles are read, at
        the address its own event proves (contract 10), never the
        server's word alone; until that is read, it says so once."""
        row, following = self._row(sid), self._following
        published = following.reading.published(sid) if following is not None else None
        if row is None or published is None:
            self._ports.say(_(PUBLISHED_NOT_LOADED))
            return
        self._view(published.address)

    def _view_naddr(self, naddr: str) -> None:
        """The publish sheet's Ansehen: the article it just published."""
        try:
            d, author, kind, _relays = decode_naddr(naddr)
        except ValueError:
            return
        self._view(f"{kind}:{author}:{d}")

    def _view(self, address: str) -> None:
        """Open the article at ``address`` (``30023:<pk>:<d>``) where
        articles are read: its author and ``d``, never the row's."""
        _kind, author, d = address.split(":", 2)
        post = web_readers.Post(kind=web_readers.ARTICLE,
                                entity=encode_naddr(d, author, formats.ARTICLE_KIND),
                                author=author, identifier=d)
        link = web_readers.link_for(web_readers.default_reader(web_readers.ARTICLE, ""), post)
        if link:
            QDesktopServices.openUrl(QUrl(link))

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
