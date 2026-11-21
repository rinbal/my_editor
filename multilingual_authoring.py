# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Linked language documents, bound to normal tabs rather than a second editor.

The writing target is always the active tab. A reference is a read-only snapshot
and can never retarget formatting, saving or publishing. Private linkage belongs
only to the tab's restart and crash records, the encrypted draft (its inner
root field) and, for a saved file, this computer's own record of it
(nostr/language_links.py): never event tags, and never the Markdown file,
which people share. Signed events are kept in memory only; they can be read
again by their id.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import hashlib
import uuid
import time
from dataclasses import replace

import shiboken6
from PySide6.QtCore import QObject, QPoint, Qt, QSize, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSplitter, QToolButton, QVBoxLayout, QWidget

from constants import DARK_FG, LIGHT_FG
from i18n import _
import format_icons
from settings import load_settings, save_setting
from nostr.events import verify_event
from nostr.language_links import LanguageLinks
from nostr.private_draft import PRIVATE_DRAFT_FIELD
from nostr.multilingual import is_translation_tag, translation_metadata, translation_tags
from nostr.content_language import normalize_language
from nostr.article_details import ArticleDetails, default_title
from nostr.ui.language_chooser import LanguageChooser, language_name
from nostr.ui import appearance

REFERENCE_MIN_WIDTH = 900
MAX_LINK_BYTES = 2 * 1024 * 1024


_PROJECTION = ("content", "title", "summary", "image", "language")


def projection_key(snapshot):
    """A short fingerprint of what a translation is made from (its text,
    title, summary, cover and language), kept with a reference whose text
    had to be left out of a draft (draft_link)."""
    values = [str(snapshot.get(field, "") or "") for field in _PROJECTION]
    values[0] = values[0].rstrip()
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode("utf-8")).hexdigest()


def same_source_projection(left, right):
    """Only writing metadata counts; links, tags and publication dates do not.
    A reference kept without its text (draft_link) compares by fingerprint."""
    if "content" not in right and right.get("projection"):
        return projection_key(left) == right["projection"]
    for field in _PROJECTION:
        before, after = left.get(field, ""), right.get(field, "")
        if field == "content":
            before, after = before.rstrip(), after.rstrip()
        if before != after:
            return False
    return True


def private_link(value):
    """A bounded, independently owned private record, or None for invalid input.

    Public status here is presentation only. Public relation emission requires
    independently verified events; a loaded local record grants no authority.
    """
    if not isinstance(value, dict) or value.get("version", 1) != 1:
        return None
    if not isinstance(value.get("groupId"), str) or not value["groupId"]:
        return None
    if not isinstance(value.get("versions"), dict):
        return None
    if not all(normalize_language(code) == code and isinstance(d, str) and d
               for code, d in value["versions"].items()):
        return None
    source = value.get("source")
    if source is not None and (not isinstance(source, dict) or not all(
            isinstance(source.get(k, ""), str)
            for k in ("dTag", "language", "title", "summary", "image", "content", "eventId", "address"))):
        return None
    try:
        raw = json.dumps(value, ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_LINK_BYTES:
            return None
        record = json.loads(raw)
        if isinstance(record.get("source"), dict):
            # A signed event is never kept: its id names it, and it is read
            # again (me68-red-a-4).
            record["source"].pop("event", None)
        view = record.get("view")
        if view is not None:
            if not isinstance(view, dict):
                record.pop("view", None)
            else:
                ratio = view.get("referenceRatio", .33)
                ratio = ratio if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and math.isfinite(ratio) else .33
                record["view"] = {"referenceHidden": view.get("referenceHidden") is True,
                                  "details": view.get("details") is True,
                                  "referenceRatio": min(.7, max(.25, ratio))}
        if "published" in record and not isinstance(record["published"], dict):
            record.pop("published")
        if "documents" in record:
            documents = record["documents"]
            record["documents"] = {key: path for key, path in documents.items()
                                   if isinstance(key, str) and isinstance(path, str) and len(path) <= 4096} if isinstance(documents, dict) else {}
        if "documentId" in record and not isinstance(record["documentId"], str):
            record.pop("documentId")
        return record
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return None




def draft_link(record, fits=lambda _record: True):
    """The link as an encrypted draft keeps it: without where the versions
    are saved on this computer (documents); the reference's text only
    while the draft still ``fits`` with it, else its fingerprint instead
    (projection_key), and the text is read again from the published
    reference or the original."""
    link = private_link(record)
    if link is None:
        return None
    link.pop("documents", None)
    for key in ("expected_event_id", "reference_unavailable"):
        link.pop(key, None)
    source = link.get("source")
    if isinstance(source, dict):
        source["projection"] = projection_key(source)
        if "content" in source and not fits(link):
            source.pop("content")
    return link


def is_document_uuid(identifier):
    """Whether ``identifier`` is one this app made for a new document (a
    UUID): such a document can't be out before it is published from here."""
    try:
        return str(uuid.UUID(identifier)) == identifier
    except (ValueError, AttributeError, TypeError):
        return False


def verified_source(source, event):
    """``event`` when it is the signed revision ``source`` names."""
    if not isinstance(source, dict) or not isinstance(event, dict) or event.get("kind") != 30023 or not verify_event(event):
        return None
    identifiers = [tag[1] for tag in event.get("tags", []) if len(tag) > 1 and tag[0] == "d"]
    if len(identifiers) != 1 or not identifiers[0]:
        return None
    address = f"30023:{event['pubkey']}:{identifiers[0]}"
    if source.get("eventId") != event["id"] or source.get("address") != address:
        return None
    return event


class ReferencePane(QFrame):
    """A private reference view; never another editable document binding."""

    review_requested = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("LanguageReference")
        self.setMinimumWidth(260)
        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 8)
        column.setSpacing(8)
        self.caption = QLabel()
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setWordWrap(True)
        column.addWidget(self.caption)
        self.change_hint = QLabel()
        self.change_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.change_hint.setWordWrap(True)
        column.addWidget(self.change_hint)
        self.review = QPushButton(_("Review"))
        self.review.clicked.connect(self.review_requested)
        column.addWidget(self.review)
        self.change_hint.hide()
        self.review.hide()
        self.unpublished = QLabel(_("Unpublished edits aren’t included."))
        self.unpublished.setWordWrap(True)
        self.unpublished.hide()
        column.addWidget(self.unpublished)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setFrameShape(QFrame.Shape.NoFrame)
        self.body.setAccessibleName(_("Reference"))
        self.body.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        column.addWidget(self.body, 1)

    def show_source(self, source, dark):
        name = language_name(source.get("language", "")) or _("Original")
        self.caption.setText(_("{language} · Reference").format(language=name))
        text = source.get("content", "")
        if text != self.body.toPlainText():
            scroll = self.body.verticalScrollBar().value()
            self.body.setPlainText(text)
            self.body.verticalScrollBar().setValue(scroll)
        appearance.neutral(self, dark)
        appearance.neutral(self.body, dark)


class MultilingualAuthoring(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.controls = QWidget()
        self.controls.setObjectName("LanguageControls")
        self.controls.hide()
        row = QHBoxLayout(self.controls)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.languages = QToolButton()
        self.languages.setDefaultAction(window.act_write_language)
        self.languages.setAccessibleName(window.act_write_language.text())
        self.reference = QToolButton()
        self.reference.setDefaultAction(window.act_language_reference)
        self.reference.setAccessibleName(window.act_language_reference.text())
        for button in (self.languages, self.reference):
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(format_icons.SIZE, format_icons.SIZE))
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            row.addWidget(button)
        self._chooser = None
        self._confirmed_public = set()
        # The signed public events read or published this session, by id.
        self._public_events = {}
        self._links = LanguageLinks()

    def _set_link(self, ed, record):
        """``record`` becomes ``ed``'s link, and a saved document's link is
        kept on this computer (language_links), never in its file."""
        ed._language_link = record
        self._keep(ed)

    def _keep(self, ed):
        record = private_link(getattr(ed, "_language_link", None))
        if record and getattr(ed, "_file_path", None):
            for key in ("expected_event_id", "reference_unavailable"):
                record.pop(key, None)
            self._links.keep(record.get("owner") or self._owner(),
                             self.window._article_details_of(ed).identifier, record)

    def remember_view(self, ed, **changes):
        record = private_link(getattr(ed, "_language_link", None)) if ed is not None else None
        if record:
            view = dict(record.get("view", {}))
            if any(view.get(key) != value for key, value in changes.items()):
                view.update(changes)
                record["view"] = view
                ed._language_link = record

    def tab_changed(self):
        if not self.window.nostr_state.active:
            return
        ed = self.window.current_editor()
        record = self.context(ed)
        if record:
            details = record.get("view", {}).get("details", False)
            self.window.act_article_details.setChecked(bool(details))

    def editors(self):
        w = self.window
        for i in range(w.tabs.count()):
            editor = w._editor_from_widget(w.tabs.widget(i))
            if editor is not None:
                yield editor

    def _owner(self):
        profile = self.window._profile_store.default()
        return profile.user_pubkey.lower() if profile is not None else ""

    def context(self, ed):
        record = private_link(getattr(ed, "_language_link", None)) if ed is not None else None
        owner = self._owner()
        if record is None and ed is not None and getattr(ed, "_file_path", None):
            # A saved document's link is this computer's (language_links).
            record = private_link(self._links.get(owner, self.window._article_details_of(ed).identifier))
            if record is not None:
                ed._language_link = copy.deepcopy(record)
        if record and not record.get("owner") and owner:
            # Older optional private metadata can omit ownership. Adopting
            # the selected account does not establish any public provenance.
            record["owner"] = owner
            ed._language_link = copy.deepcopy(record)
        if record and (record.get("owner") or owner) == owner:
            if record.get("source"):
                record["source"]["public"] = self._public_event(record["source"]) is not None
            event = getattr(ed, "_published_event", None)
            remembered = record.get("published", {}).get("eventId", "")
            known = (event["id"] if isinstance(event, dict) and verify_event(event)
                     else remembered if isinstance(remembered, str) and re.fullmatch(r"[0-9a-f]{64}", remembered)
                     else None)
            record.pop("expected_event_id", None)
            if known:
                record["expected_event_id"] = known
            elif is_document_uuid(self.window._article_details_of(ed).identifier or self.identity(ed)):
                # Made here and never published from here: not out yet.
                record["expected_event_id"] = None
            # Otherwise not known (an article opened from its file, public
            # perhaps): "not known" never becomes "not out yet" (red-a-2).
            record["reference_unavailable"] = getattr(ed, "_language_source_check", {}).get("available") is False
            return record
        return None

    def _public_event(self, source):
        """The signed public revision ``source`` names, when this session
        saw it public (note_public), else None."""
        event = self._public_events.get((source or {}).get("eventId", ""))
        return verified_source(source, event) if event is not None else None

    def identity(self, ed):
        record = private_link(getattr(ed, "_language_link", None))
        return (record or {}).get("documentId") or self.window._article_details_of(ed).identifier or getattr(ed, "_language_document_id", "")

    def source_editor(self, ed):
        record = self.context(ed)
        source_id = (record or {}).get("source", {}).get("dTag")
        return next((item for item in self.versions(ed)
                     if self.identity(item) == source_id), None)

    def versions(self, ed):
        record = self.context(ed)
        return [item for item in self.editors() if record and self.context(item)
                and self.context(item)["groupId"] == record["groupId"]]

    def choose(self, *, edit_current=False):
        w, ed = self.window, self.window.current_editor()
        if ed is None or not w._can_be_an_article(ed):
            return
        current = w._article_details_of(ed).language
        record = self.context(ed)
        recent = load_settings().get("article_recent_languages", [])
        chooser = LanguageChooser(current=current, existing=(record or {}).get("versions", {}),
                                  recent=recent if isinstance(recent, list) else [],
                                  dark=w.is_dark_theme, parent=w)
        if edit_current:
            chooser.setWindowTitle(_("Article language"))
            chooser.chosen.connect(lambda code, source=ed: self.set_language(source, code))
        else:
            chooser.chosen.connect(lambda code, source=ed: self.open_language(source, code))
        chooser.move(self.languages.mapToGlobal(QPoint(0, self.languages.height())))
        self._chooser = chooser
        chooser.open()
        chooser.search.setFocus()

    def set_language(self, ed, code):
        """Correct a label while keeping existing language-document identities."""
        code = normalize_language(code)
        if not code or not shiboken6.isValid(ed):
            return
        details = self.window._article_details_of(ed)
        record = self.context(ed)
        identity = self.identity(ed)
        if record and code in record["versions"] and record["versions"][code] != identity:
            self.open_language(ed, code)
            return
        self.window._set_article_details(ed, details.with_language(code))
        if record:
            versions = {language: identifier for language, identifier in record["versions"].items()
                        if identifier != identity}
            versions[code] = identity
            for sibling in self.versions(ed):
                own = self.context(sibling)
                own["versions"] = dict(versions)
                self._set_link(sibling, own)
                sibling.document().setModified(True)
        self.refresh()

    def _snapshot(self, ed, language):
        details = self.window._article_details_of(ed)
        event = getattr(ed, "_published_event", None)
        public = isinstance(event, dict) and verify_event(event) and event["id"] in self._confirmed_public
        shown_details = ArticleDetails.from_tags(event.get("tags", []), signer=event["pubkey"]) if public else details
        content = (str(event.get("content", "")) if public
                   else self.window._publish_payload(ed, "markdown", keep_local=True)[0])
        # Existing public source uses its actual published revision, never
        # silently pins unsaved local changes as though they were public.
        snapshot = {"dTag": details.identifier or self.identity(ed), "language": shown_details.language if public else language,
                    "title": shown_details.title or default_title(content), "summary": shown_details.summary, "image": shown_details.image,
                    "content": content,
                    "eventId": str(event.get("id", "")) if public else "",
                    "address": f"30023:{event['pubkey']}:{details.identifier}" if public else "",
                    "public": public}
        snapshot["projection"] = projection_key(snapshot)
        return snapshot

    def open_language(self, source_ed, code):
        w = self.window
        code = normalize_language(code)
        if not code or not shiboken6.isValid(source_ed):
            return None
        raw_link = private_link(getattr(source_ed, "_language_link", None))
        if raw_link and raw_link.get("owner") and raw_link["owner"] != self._owner():
            w.status.showMessage(_("Use the account that owns these versions."), 10000, nostr=True)
            return None
        source_details = w._article_details_of(source_ed)
        if code == source_details.language:
            w.tabs.setCurrentIndex(w._editor_widget_index(source_ed))
            return source_ed
        record = self.context(source_ed)
        if record and code in record["versions"]:
            target_id = record["versions"][code]
            for target in self.versions(source_ed):
                if self.identity(target) == target_id:
                    w.tabs.setCurrentIndex(w._editor_widget_index(target))
                    target.setFocus()
                    return target
            path = record.get("documents", {}).get(target_id, "")
            if isinstance(path, str) and os.path.isfile(path):
                return w.open_path(path)
            if w._draft_store.get(target_id) is not None:
                w._on_panel_open_draft(target_id)
                return w.current_editor()
        # Source language is an article property. An unlabeled original stays
        # unknown; selecting a target never guesses from interface language
        # or adds a second setup question.
        source_lang = source_details.language
        source_id = source_details.identifier or self.identity(source_ed) or str(uuid.uuid4())
        source_ed._language_document_id = source_id
        if not source_details.identifier:
            source_details = source_details.with_changes(identifier=source_id)
        w._set_article_details(source_ed, source_details)
        group = record["groupId"] if record else str(uuid.uuid4())
        versions = dict(record["versions"]) if record else ({source_lang: source_id} if source_lang else {})
        target_id = str(uuid.uuid4())
        versions[code] = target_id
        common = {"version": 1, "groupId": group, "owner": self._owner(), "versions": versions}
        for sibling in self.editors():
            existing = self.context(sibling)
            if sibling is source_ed or existing and existing["groupId"] == group:
                self._set_link(sibling, {**(existing or common), "versions": dict(versions),
                                         "documentId": (existing or {}).get("documentId") or self.identity(sibling)})
                sibling.document().setModified(True)
        source = self._snapshot(source_ed, source_lang)
        source["dTag"] = source_id
        target = w.new_tab()
        # Carry common optional details once. Public relation identity,
        # source pins and old-language text never become a target's values.
        inherited = replace(source_details, identifier=target_id, title="", summary="",
                            published_at=None, carried=(), kept=frozenset())
        w._set_article_details(target, inherited.with_language(code))
        self._set_link(target, {**common, "source": source, "documentId": target_id, "view": {"details": False}})
        target._language_reference_visible = True
        target._language_reference_narrow = False
        target.setPlaceholderText(_("Write or paste your {language} version.").format(language=language_name(code)))
        target.setAccessibleDescription(_("Writing in {language}").format(language=language_name(code)))
        w.act_article_details.setChecked(False)
        # Reference is the supplementary pane; don't retain a third preview.
        w._choose_view("write")
        recent = load_settings().get("article_recent_languages", [])
        recent = recent if isinstance(recent, list) else []
        save_setting("article_recent_languages", list(dict.fromkeys((code, *recent)))[:8])
        self.refresh()
        target.setFocus()
        # An original opened from its file may be public already: then its
        # published revision is the reference (red-a-2).
        self._look_up_public(source_ed, lambda t=target: self._repin_source(t))
        return target

    def _look_up_public(self, ed, finished):
        """Read once whether the article in ``ed`` is public already, when
        that is not known (an article opened from its file, from Drafts,
        after a restart): found and verified, it is ``ed``'s published
        version. ``finished()`` then, found or not; at once when there is
        nothing to read. Quiet: no status, no message."""
        if not self._needs_public_lookup(ed):
            finished()
            return
        ed._language_public_looked_up = True
        owner = self._owner()
        identifier = self.window._article_details_of(ed).identifier
        from nostr.outbox import defaults
        from nostr.outbox.lookup import fetch_addressable
        from nostr.outbox.policy import dedupe_relays
        from nostr.source_provenance import check_source_snapshot

        def proved(proof):
            if not shiboken6.isValid(ed) or owner != self._owner():
                return
            event = proof.get("current") if proof.get("available") else None
            if event is not None and self._address_of(event) == f"30023:{owner}:{identifier}":
                self.note_public(event)
                ed._published_event = copy.deepcopy(event)
                own = private_link(getattr(ed, "_language_link", None))
                if own:
                    own["published"] = self._published_record(event)
                    self._set_link(ed, own)
            finished()

        def found(results):
            if not shiboken6.isValid(ed) or owner != self._owner():
                return
            event = results[identifier].event if identifier in results else None
            if event is None:
                finished()
                return
            snapshot = {"address": f"30023:{owner}:{identifier}", "eventId": event["id"],
                        "content": event["content"]}
            ed._language_public_check = check_source_snapshot(
                self.window._relay_pool, self.window._relay_directory, snapshot, proved, parent=ed)

        def planned(plan):
            if not shiboken6.isValid(ed):
                return
            ed._language_public_lookup = fetch_addressable(
                self.window._relay_pool, dedupe_relays(plan.targets, defaults.INDEXER_RELAYS),
                kind=30023, author=owner, d_tags=[identifier], on_done=found, parent=ed)
        self.window._relay_directory.publish_plan(owner, planned)

    def _needs_public_lookup(self, ed):
        """Whether it is not known yet whether the article in ``ed`` is out:
        it has a link name not made here, and no published version known."""
        if ed is None or not shiboken6.isValid(ed) or not self._owner():
            return False
        identifier = self.window._article_details_of(ed).identifier
        record = self.context(ed)
        return bool(identifier and not is_document_uuid(identifier)
                    and not isinstance(getattr(ed, "_published_event", None), dict)
                    and not (record or {}).get("published", {}).get("eventId")
                    and not getattr(ed, "_language_public_looked_up", False))

    @staticmethod
    def _address_of(event):
        ds = [t[1] for t in event.get("tags", []) if len(t) > 1 and t[0] == "d"]
        return f"30023:{event.get('pubkey', '')}:{ds[0]}" if len(ds) == 1 else ""

    def _published_record(self, event):
        """What a document's link keeps of its published version."""
        return {"eventId": event.get("id", ""), "address": self._address_of(event),
                "root": translation_metadata(event.get("tags", []))["root"]}

    def _repin_source(self, target):
        """The original turned out to be public: a reference not yet pinned
        to a published revision becomes that revision."""
        if not shiboken6.isValid(target):
            return
        record = self.context(target)
        source = (record or {}).get("source")
        source_ed = self.source_editor(target)
        if not source or source.get("eventId") or source_ed is None:
            return
        event = getattr(source_ed, "_published_event", None)
        if not (isinstance(event, dict) and self._public_event({"eventId": event.get("id"),
                                                                 "address": self._address_of(event)})):
            return
        pinned = self._snapshot(source_ed, source.get("language", ""))
        pinned["dTag"] = source.get("dTag", pinned["dTag"])
        record["source"] = pinned
        self._set_link(target, record)
        self.refresh()

    def file_saved(self, ed, path):
        """A version was saved at ``path``: every version's link knows where
        it is (to open it from the others), and each saved one's link is
        kept on this computer; the file itself holds none of it."""
        record = self.context(ed)
        if not record:
            return
        documents = dict(record.get("documents", {}))
        for sibling in self.versions(ed):
            identifier = self.window._article_details_of(sibling).identifier
            own_path = path if sibling is ed else getattr(sibling, "_file_path", None)
            if identifier and own_path:
                documents[identifier] = os.path.abspath(own_path)
        for sibling in self.versions(ed):
            own = self.context(sibling)
            own["documents"] = dict(documents)
            self._set_link(sibling, own)

    def note_public(self, event):
        """Called only by verified public ingestion or an accepted publication."""
        if isinstance(event, dict) and event.get("kind") == 30023 and verify_event(event):
            self._confirmed_public.add(event["id"])
            self._public_events[event["id"]] = copy.deepcopy(event)

    def _schedule_source_check(self, ed, record):
        """Check on first open/restore, then at most once per two minutes."""
        source = record.get("source", {})
        if not source.get("eventId") or not source.get("address"):
            return
        token = (self._owner(), record["groupId"], source["eventId"])
        if (getattr(ed, "_language_last_source_token", None) == token
                and time.monotonic() - getattr(ed, "_language_source_checked_at", 0) < 120):
            return
        if getattr(ed, "_language_preparing", False) or getattr(ed, "_language_source_scheduled", False):
            return
        ed._language_source_scheduled = True
        def check():
            ed._language_source_scheduled = False
            current = self.context(ed)
            if current and (self._owner(), current["groupId"], current.get("source", {}).get("eventId")) == token:
                self.prepare_source(ed, lambda ready: None, quiet=True)
        QTimer.singleShot(0, ed, check)

    def prepare_source(self, ed, done, *, quiet=False):
        """One bounded read shared by quiet reference updates and send actions.

        A foreground action joins an in-flight quiet read; it is never lost or
        duplicated. Quiet reads do not write a global status or open a sheet.
        """
        record = self.context(ed)
        source = (record or {}).get("source", {})
        if ed is None or not record:
            done(True)
            return
        if getattr(ed, "_language_preparing", False):
            if not quiet:
                # The latest action asked for is the one that follows
                # (Publish, then Submit: Submit opens), red-a-11.
                ed._language_prepare_callback = done
                self.window.status.showMessage(_("Checking the published version…"), nostr=True)
            return
        original = self.source_editor(ed) if source else ed
        if not quiet and not source.get("eventId") and self._needs_public_lookup(original):
            # Whether the original is out already is read first (red-a-2).
            self.window.status.showMessage(_("Checking the published version…"), nostr=True)
            self._look_up_public(original, lambda: (self._repin_source(ed) if source else None,
                                                    self.prepare_source(ed, done, quiet=quiet)))
            return
        source = (self.context(ed) or {}).get("source", {})
        if source and (not source.get("address") or not source.get("eventId")):
            self.window.status.clearMessage()
            done(True)
            return
        owner, group, document_id = self._owner(), record["groupId"], self.identity(ed)
        token = (owner, group, source.get("eventId", ""))
        ed._language_preparing = True
        ed._language_prepare_callback = None if quiet else done
        if not quiet:
            self.window.status.showMessage(_("Checking the published version…"), nostr=True)

        def still_owned():
            current = self.context(ed)
            valid = (owner == self._owner() and document_id == self.identity(ed)
                     and group == (current or {}).get("groupId", "")
                     and source.get("eventId", "") == (current or {}).get("source", {}).get("eventId", ""))
            if not valid:
                foreground = getattr(ed, "_language_prepare_callback", None) is not None
                ed._language_preparing = False
                ed._language_prepare_callback = None
                if foreground:
                    self.window.status.showMessage(_("The reference changed. Try again."), 10000, nostr=True)
            return valid

        def complete():
            callback = getattr(ed, "_language_prepare_callback", None)
            ed._language_prepare_callback = None
            ed._language_preparing = False
            ed._language_last_source_token = token
            ed._language_source_checked_at = time.monotonic()
            if callback is not None:
                callback(True)
            else:
                done(True)

        if not source:
            from nostr.source_provenance import check_linked_siblings
            def siblings_checked(result):
                if not shiboken6.isValid(ed) or not still_owned():
                    return
                if getattr(ed, "_language_prepare_callback", None) is not None:
                    self.window.status.clearMessage()
                ed._language_group_check = result
                for event in result.get("events", []):
                    self.note_public(event)
                complete()
            ed._language_group_lookup = check_linked_siblings(
                self.window._relay_pool, self.window._relay_directory, record["versions"], owner,
                siblings_checked, exclude=self.window._article_details_of(ed).identifier, parent=ed)
            return
        from nostr.source_provenance import check_source_snapshot
        def checked(result):
            if not shiboken6.isValid(ed) or not still_owned():
                return
            foreground = getattr(ed, "_language_prepare_callback", None) is not None
            if foreground:
                self.window.status.clearMessage()
            result["group_conflict"] = self._source_group_conflict(ed, result.get("confirmed_root", ""))
            ed._language_source_check = result
            event = result.get("event")
            if result.get("available") and event:
                self.note_public(event)
                own = private_link(getattr(ed, "_language_link", None))
                if own and own.get("source", {}).get("eventId") == source["eventId"]:
                    # A reference whose text a draft left out reads it here.
                    own["source"].setdefault("content", event.get("content", ""))
                    own["source"]["public"] = True
                    self._set_link(ed, own)
            elif foreground:
                self.window.status.showMessage(_("The reference could not be verified. Try again."), 10000, nostr=True)
            self.refresh()
            complete()  # Missing references never escalate publication scope.
        ed._language_source_lookup = check_source_snapshot(
            self.window._relay_pool, self.window._relay_directory, source, checked, parent=ed)

    def review_source(self, ed):
        """Review concrete old/new writing; advancing the pin is explicit."""
        record = self.context(ed)
        source = (record or {}).get("source")
        result = getattr(ed, "_language_source_check", {})
        current = result.get("current")
        if result.get("group_conflict") or self._source_group_conflict(ed, result.get("confirmed_root", "")):
            self.window.status.showMessage(_("This reference is linked to another article. Your current reference is kept."), 10000, nostr=True)
            return
        source_ed = self.source_editor(ed)
        if current:
            candidate = self._snapshot(source_ed, source.get("language", "")) if source_ed else dict(source)
            from nostr.content_language import language_from_tags
            identifier = next((t[1] for t in current.get("tags", []) if len(t) > 1 and t[0] == "d"), "")
            current_details = ArticleDetails.from_tags(current.get("tags", []), signer=current.get("pubkey", ""))
            candidate.update(content=current.get("content", ""), title=current_details.title,
                             summary=current_details.summary, image=current_details.image,
                             eventId=current["id"], event=current,
                             address=f"30023:{current['pubkey']}:{identifier}", public=True,
                             language=language_from_tags(current.get("tags", [])))
        elif source_ed is not None and source and not source.get("eventId"):
            candidate = self._snapshot(source_ed, self.window._article_details_of(source_ed).language)
        else:
            return
        sheet = QDialog(self.window)
        sheet.setWindowTitle(_("Review reference"))
        sheet.resize(850, 540)
        outer = QVBoxLayout(sheet)
        panes = QSplitter(Qt.Orientation.Horizontal)
        for caption, snapshot in ((_("Previous reference"), source),
                                  (_("Current reference"), candidate)):
            page = QWidget()
            column = QVBoxLayout(page)
            column.addWidget(QLabel(caption))
            metadata = QLabel("\n".join(part for part in (snapshot.get("title", ""), snapshot.get("summary", ""), snapshot.get("image", "")) if part))
            metadata.setTextFormat(Qt.TextFormat.PlainText)
            metadata.setWordWrap(True)
            column.addWidget(metadata)
            body = QPlainTextEdit(snapshot.get("content", ""))
            body.setReadOnly(True)
            body.setAccessibleName(caption)
            column.addWidget(body)
            panes.addWidget(page)
        outer.addWidget(panes, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        use = buttons.addButton(_("Use this reference"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(sheet.reject)
        def accepted():
            latest = self.context(ed)
            if latest:
                latest["source"] = {key: value for key, value in copy.deepcopy(candidate).items()
                                    if key != "event"}
                latest["source"]["projection"] = projection_key(latest["source"])
                self._set_link(ed, latest)
                ed._language_source_check = {"available": True, "event": candidate.get("event"),
                                             "current": candidate.get("event"), "changed": False,
                                             "confirmed_root": result.get("confirmed_root", "")}
                if candidate.get("event"):
                    ed._language_last_source_token = (self._owner(), latest["groupId"], candidate["eventId"])
                    ed._language_source_checked_at = time.monotonic()
                    self.note_public(candidate["event"])
                    self.window._set_article_details(ed, self.publication_details(ed))
                ed.document().setModified(True)
                self.refresh()
            sheet.accept()
        use.clicked.connect(accepted)
        outer.addWidget(buttons)
        appearance.neutral(sheet, self.window.is_dark_theme)
        sheet.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._review_sheet = sheet
        sheet.open()

    def publication_details(self, ed):
        """Public tags use the recorded verified revision, never private IDs."""
        details = self.window._article_details_of(ed)
        record = self.context(ed)
        source = (record or {}).get("source", {})
        event = self._public_event(source) if source else None
        if event is None:
            if not source and record:
                evidence = getattr(ed, "_language_group_check", {})
                if not evidence.get("conflict") and evidence.get("events"):
                    own = f"30023:{record.get('owner') or self._owner()}:{details.identifier}"
                    root = evidence.get("root", "")
                    translations = list(evidence.get("translations", []))
                    if root == own:
                        # This version is the group's root: it names its
                        # verified versions, never itself as its own
                        # original (red-a-3).
                        root = ""
                        translations = [self._address_of(item) for item in evidence["events"]]
                    tags = translation_tags({"root": root, "translations": sorted(
                        {address for address in translations if address and address != own})})
                    carried = tuple(tag for tag in details.carried if not is_translation_tag(tag))
                    return details.with_changes(carried=carried + tuple(tuple(tag) for tag in tags))
            return details
        source_address = source.get("address", "")
        check = getattr(ed, "_language_source_check", {})
        if self._source_group_conflict(ed, check.get("confirmed_root", "")):
            return details
        root = check.get("confirmed_root", "")
        if not check and not root and not translation_metadata(event.get("tags", []))["root"]:
            root = source_address
        tags = translation_tags({"root": root, "source": source_address,
                                 "source_event": event["id"]})
        carried = tuple(tag for tag in details.carried if not is_translation_tag(tag))
        return details.with_changes(carried=carried + tuple(tuple(tag) for tag in tags))

    def pair_source(self, ed):
        """Freeze the actual reference for the explicit two-version review."""
        source_ed = self.source_editor(ed)
        record = self.context(ed)
        if source_ed is None or not record or not record.get("source"):
            return None
        details = self.publication_details(source_ed)
        body, media = self.window._publish_payload(source_ed, "markdown", keep_local=True)
        body = body.rstrip()
        source = record["source"]
        problem = ""
        projection = {"content": body, "title": details.title or default_title(body), "summary": details.summary,
                      "image": details.image, "language": details.language}
        if not same_source_projection(projection, source):
            problem = _("The original changed. Review the reference before publishing both.")
        if "asset:" in body or "file:" in body or details.image.startswith(("asset:", "file:")):
            problem = _("The original has files that need publishing. Open the original to prepare them.")
        if not body.strip():
            problem = _("The original is empty. Open it to finish writing.")
        if self._source_group_conflict(ed, getattr(ed, "_language_source_check", {}).get("confirmed_root", "")):
            problem = _("This reference is linked to another article. Your current reference is kept.")
        pair = {"body": body, "details": details, "media": tuple(media),
                "language": details.language, "problem": problem, "editor": source_ed,
                "owner": self._owner()}
        original = self.context(source_ed) or {}
        if "expected_event_id" in original:
            # Known: the original's published version, or not out yet.
            pair["expected_event_id"] = original["expected_event_id"]
        return pair

    def _source_group_conflict(self, ed, confirmed_root):
        record = self.context(ed)
        published = (record or {}).get("published", {})
        is_public = bool(getattr(ed, "_published_event", None) or published.get("eventId"))
        root = (translation_metadata(self.window._article_details_of(ed).carried)["root"]
                or published.get("root", ""))
        return bool(is_public and root and root != confirmed_root)

    def source_published(self, target, event):
        """Retain accepted pair progress before its target asks a signer.

        Only the intended target's reviewed projection is eligible. Publishing
        a changed original elsewhere never silently advances its source pin.
        """
        record = self.context(target)
        source = (record or {}).get("source")
        if not source or not isinstance(event, dict) or not verify_event(event):
            return
        details = ArticleDetails.from_tags(event.get("tags", []), signer=event.get("pubkey", ""))
        if source.get("dTag") != details.identifier or event.get("pubkey") != record.get("owner"):
            return
        projection = {"content": event["content"].rstrip(), "title": details.title,
                      "summary": details.summary, "image": details.image, "language": details.language}
        if not same_source_projection(projection, source):
            return
        source.update(eventId=event["id"], address=f"30023:{event['pubkey']}:{details.identifier}",
                      public=True)
        record["source"] = source
        self._set_link(target, record)
        self.note_public(event)
        target._language_last_source_token = (self._owner(), record["groupId"], event["id"])
        target._language_source_checked_at = time.monotonic()
        target._language_source_check = {"available": True, "event": event, "current": event,
                                         "changed": False, "confirmed_root": translation_metadata(event["tags"])["root"] or source["address"]}
        self.window._set_article_details(target, self.publication_details(target))
        target.document().setModified(True)
        backup = getattr(target, "_backup", None)
        if backup is not None:
            backup.write_now()

    def published(self, ed, event):
        """Accepted signed publication only, called by the ordinary publish flow."""
        if not isinstance(event, dict) or not verify_event(event):
            return
        self.note_public(event)
        ed._published_event = copy.deepcopy(event)
        # The document's details stay as they are: the link name and first
        # date go back through the publish sheet (details), and nothing of
        # the wire is written into a file (red-a-5).
        record = self.context(ed)
        if record:
            record["published"] = self._published_record(event)
            self._set_link(ed, record)

    def remember_reference(self, splitter):
        for ed in self.editors():
            pane = getattr(ed, "_language_reference_pane", None)
            if getattr(ed, "_view_splitter", None) is splitter and pane is not None and pane.isVisible():
                sizes = splitter.sizes()
                text, reference = sizes[0], sizes[splitter.indexOf(pane)]
                if text > 0 and reference > 0:
                    ed._language_reference_ratio = reference / (text + reference)
                    self.remember_view(ed, referenceRatio=ed._language_reference_ratio)
                return True
        return False

    def toggle_reference(self, *_args):
        ed = self.window.current_editor()
        if not self.context(ed) or not self.context(ed).get("source"):
            return
        narrow = ed._view_splitter.width() < REFERENCE_MIN_WIDTH
        if narrow:
            ed._language_reference_narrow = not getattr(ed, "_language_reference_narrow", False)
            ed._language_reference_visible = True
        else:
            ed._language_reference_visible = not getattr(ed, "_language_reference_visible", True)
        self.remember_view(ed, referenceHidden=not getattr(ed, "_language_reference_visible", True))
        self.window.act_article_details.setChecked(False)
        self.window._choose_view("write")
        self.refresh()

    def refresh(self):
        w, ed = self.window, self.window.current_editor()
        active = bool(getattr(w.nostr_state, "active", False))
        # Write, Preview and Split stay as they are for every document; the
        # language controls join them only on a tab that can be an article
        # (red-a-10, red-b-3).
        if not active:
            self.controls.hide()
            w.act_write_language.setVisible(False)
            w.act_language_reference.setVisible(False)
            pane = getattr(ed, "_language_reference_pane", None) if ed is not None else None
            if pane is not None:
                pane.hide()
                if w._shown_view(ed) == "write":
                    ed._view_splitter.widget(0).show()
            return
        raw_link = private_link(getattr(ed, "_language_link", None)) if ed is not None else None
        owned = not raw_link or not raw_link.get("owner") or raw_link["owner"] == self._owner()
        enabled = owned and ed is not None and w._can_be_an_article(ed)
        self.controls.setVisible(enabled)
        self.languages.setVisible(enabled)
        w.act_write_language.setVisible(active)
        w.act_write_language.setEnabled(enabled)
        record = self.context(ed)
        source = (record or {}).get("source")
        if record and not hasattr(ed, "_language_reference_visible"):
            view = record.get("view", {})
            ed._language_reference_visible = not view.get("referenceHidden", False)
            ed._language_reference_ratio = view.get("referenceRatio", .33)
        has_source = bool(source)
        w.act_language_reference.setVisible(has_source and active)
        w.act_language_reference.setEnabled(has_source and enabled)
        self.reference.setVisible(has_source and enabled)
        ink = QColor(DARK_FG if w.is_dark_theme else LIGHT_FG)
        w.act_write_language.setIcon(format_icons.languages(ink))
        w.act_language_reference.setIcon(format_icons.reference(ink))
        lang = w._article_details_of(ed).language if ed is not None else ""
        label = _("Write in another language…")
        if lang:
            label += " · " + language_name(lang)
        w.act_write_language.setToolTip(label)
        self.languages.setAccessibleName(label)
        if ed is None:
            return
        pane = getattr(ed, "_language_reference_pane", None)
        if not has_source:
            if pane is not None:
                pane.hide()
            return
        if pane is None:
            pane = ReferencePane(ed._view_splitter)
            ed._language_reference_pane = pane
            ed._language_reference_initialized = False
            pane.review_requested.connect(lambda target=ed: self.review_source(target))
            ed._view_splitter.addWidget(pane)
        pane.show_source(source, w.is_dark_theme)
        self._schedule_source_check(ed, record)
        result = getattr(ed, "_language_source_check", {})
        changed = bool(result.get("changed"))
        source_ed = self.source_editor(ed)
        unpublished = False
        if source_ed is not None and source.get("eventId"):
            event = getattr(source_ed, "_published_event", None) or source.get("event")
            if isinstance(event, dict) and verify_event(event) and event["id"] in self._confirmed_public:
                published_details = ArticleDetails.from_tags(event.get("tags", []), signer=event["pubkey"])
                published = {"content": event["content"], "title": published_details.title or default_title(event["content"]),
                             "summary": published_details.summary, "image": published_details.image, "language": published_details.language}
                live_details = w._article_details_of(source_ed)
                live_body = w._publish_payload(source_ed, "markdown", keep_local=True)[0]
                live = {"content": live_body, "title": live_details.title or default_title(live_body),
                        "summary": live_details.summary, "image": live_details.image, "language": live_details.language}
                unpublished = source_ed.document().isModified() and not same_source_projection(live, published)
        pane.unpublished.setVisible(unpublished)
        if source_ed is not None and not source.get("eventId"):
            changed = not same_source_projection(self._snapshot(source_ed, w._article_details_of(source_ed).language), source)
        hint = (_("This reference is linked to another article. Your current reference is kept.")
                if result.get("group_conflict") else
                _("{language} version changed").format(language=language_name(source.get("language", "")) or _("Original")))
        pane.change_hint.setText(hint)
        pane.change_hint.setVisible(changed)
        pane.review.setVisible(changed)
        narrow = ed._view_splitter.width() < REFERENCE_MIN_WIDTH
        wants = getattr(ed, "_language_reference_visible", True)
        reference_only = narrow and getattr(ed, "_language_reference_narrow", False)
        available = w._shown_view(ed) == "write" and not w.act_article_details.isChecked() and active
        shown = available and wants and (not narrow or reference_only)
        was_shown = pane.isVisible()
        pane.setVisible(shown)
        if w._shown_view(ed) == "write":
            ed._view_splitter.widget(0).setVisible(not reference_only or not available)
        if shown and not narrow and (not getattr(ed, "_language_reference_initialized", False) or not was_shown or pane.width() < 260):
            splitter = ed._view_splitter
            width = splitter.width()
            ratio = min(.7, max(.25, float(getattr(ed, "_language_reference_ratio", .33))))
            sizes = [0] * splitter.count()
            sizes[splitter.indexOf(pane)] = max(260, round(width * ratio))
            sizes[0] = max(400, width - sizes[splitter.indexOf(pane)])
            splitter.setSizes(sizes)
            ed._language_reference_initialized = True
        name = language_name(lang)
        reference_label = (_("Back to {language}").format(language=name) if reference_only
                           else _("Show reference") if narrow or not wants else _("Hide reference"))
        w.act_language_reference.setToolTip(reference_label)
        self.reference.setAccessibleName(reference_label)
        blocked = w.act_language_reference.blockSignals(True)
        w.act_language_reference.setChecked(shown)
        w.act_language_reference.blockSignals(blocked)
