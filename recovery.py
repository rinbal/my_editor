#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Crash recovery: debounced backup files for every open editor.

Each editor gets an EditorBackup instance that writes a JSON snapshot
to ~/.cache/my_editor/backups/ a few seconds after the last keystroke.
On a normal close the backup is deleted; on a crash it survives and is
restored silently on the next launch.

Snapshots are HTML, not plain text. A plain-text snapshot restored over
an .html original and saved erased every image in the document, which is
the one thing a crash-recovery feature must never do. The record carries
a version so a build that predates this format leaves a newer file
alone, and a source mtime so a backup older than the file on disk can be
recognised and restored as a copy instead of overwriting newer work.

HTML keeps every typed character, but not everything around it: quote
levels, code blocks (with their language) and inline code come back as
plain paragraphs and plain words. The record keeps those few things in a
small list of its own ("structure": per paragraph, and the stretches of
inline code), and the restore puts them back over the HTML, once the
restored text is checked to be the very text they were taken from.
Restoring from Markdown instead would read typed text as Markdown: a
typed <br> would cut the document there. An older build ignores the list
and restores the HTML; a record from a build that kept Markdown instead
is restored from that only when it gives back exactly the HTML's text.

The record also keeps what the tab is besides its text (tab_identity):
the Nostr draft it is bound to and the published article it edits, so a
recovered tab saves that draft again and publishes that article again,
rather than starting a new one under another identifier.
"""

import dataclasses
import hashlib
import json
import os
import re
import time
import uuid

from PySide6.QtCore import QTimer

from PySide6.QtGui import QTextCursor, QTextDocument, QTextFormat

import front_matter
import user_dirs
import tab_details
import rich_text
from atomic_file import write_text
from doc_walk import iter_blocks, iter_image_names
from export_html import normalize_after_set_html
import photo_group
import photo_group_find
from markdown_writer import holds_faithfully, read_markdown
from nostr.media.assets import ASSET_SCHEME


# A folder of its own (a test's); None: backups in the person's cache folder,
# found each time (user_dirs).
BACKUP_DIR: str | None = None


def backup_dir() -> str:
    return BACKUP_DIR or str(user_dirs.cache_dir() / "backups")

# How a document's backup stands (EditorBackup.problem): kept, or why not.
KEPT = ""
WRITE_FAILED = "write failed"      # the disk refused it (full, read-only, gone)
TOO_LARGE = "too large"            # bigger than MAX_BACKUP_BYTES
# A backup is written two seconds after the last change, and at least every
# fifteen seconds while the typing goes on.
_DEBOUNCE_MS = 2_000
_MAX_INTERVAL_MS = 15_000

# Current record format. Readers must skip anything higher (see
# is_restorable) rather than guess at its meaning.
BACKUP_VERSION = 2
# The format of a locked document's record (locked_files.py): its HTML
# record, sealed, with only where it goes and when beside it. A build that
# knows "html" alone leaves it on disk, as it leaves any it does not know.
SEALED = "sealed"

# Truncated HTML is corrupt HTML, so an oversized snapshot is skipped
# whole and the previous backup is kept. The cooldown stops a huge
# document from re-serializing on every debounce tick.
MAX_BACKUP_BYTES = 32 * 1024 * 1024
_OVERSIZE_RETRY_S = 30.0

# Inline images above this size are handed to the asset layer instead of
# being written into the snapshot again and again. Smaller ones are left
# alone: the rewrite costs more than the bytes save.
MIN_EXTERNALIZE_BYTES = 64 * 1024

_DATA_URI_SRC_RE = re.compile(r'src="(data:[^"]*;base64,[^"]*)"')
_ASSET_KEY_RE = re.compile(re.escape(ASSET_SCHEME) + r":[0-9a-f]{64}")


def _ensure_backup_dir() -> None:
    os.makedirs(backup_dir(), exist_ok=True)


def _backup_id_for(file_path: str | None) -> str:
    """Stable ID derived from the file path, or a fresh UUID for untitled docs."""
    if file_path:
        return hashlib.md5(file_path.encode()).hexdigest()
    return str(uuid.uuid4())


def _backup_path_for(backup_id: str) -> str:
    return os.path.join(backup_dir(), f"{backup_id}.autosave")


def _source_mtime_ns(file_path: str | None) -> int | None:
    if not file_path:
        return None
    try:
        return os.stat(file_path).st_mtime_ns
    except OSError:
        return None


def tab_identity(editor) -> dict:
    """What a tab is besides its text, for its backup and its restart
    record: the Nostr draft it is bound to (``draft``, the binding's
    fields), the published article it edits (``article``, its tags, and
    ``article_author``, whose it is), the article details set in the
    tab (``details``, as tags: Article Details, the publish window, a
    file's front matter), its file's front matter as written, and the line
    ending its file's text is saved with (``newline``). Empty for a plain
    document."""
    identity: dict = {}
    binding = getattr(editor, "_draft_binding", None)
    if binding is not None:
        identity["draft"] = dataclasses.asdict(binding)
    article = getattr(editor, "_published_article", None)
    if article is not None:
        identity["article"] = article.tags()
        identity["article_author"] = getattr(editor, "_published_author", "")
    details = tab_details.set_in_tab(editor)
    if details is not None:
        identity["details"] = details.tags()
    front = getattr(editor, "_front_matter", None)
    if front is not None:
        # A Markdown file's front matter as written, every byte of it.
        identity["front_matter"] = front_matter.join(front, "")
    newline = getattr(editor, "_newline", None)
    if newline:
        identity["newline"] = newline
    return identity


def document_is_empty(doc) -> bool:
    """True when a document holds nothing worth keeping: no text, no image.

    The one test for it: an empty document's HTML is still a full skeleton,
    so the length of its HTML says nothing.
    """
    return doc.characterCount() <= 1 and not any(True for _ in iter_image_names(doc))


def is_restorable(record: dict) -> bool:
    """Whether this build can read a backup record.

    A record without a version is the old plain-text format. One written
    by a newer build, or in a format this build does not know, is left on
    disk untouched rather than guessed at.
    """
    version = record.get("version")
    if version is None:
        return True
    return (isinstance(version, int) and not isinstance(version, bool)
            and version <= BACKUP_VERSION and record.get("format") in ("html", SEALED))


def unseal_backup(record: dict, password: str):
    """A sealed record's own (an "html" record), and the lock its document
    goes on with. Raises locked_files.WrongPassword."""
    from locked_files import Lock
    lock, data = Lock.open_backup(record["locked"], password)
    opened = json.loads(data.decode("utf-8"))
    opened["_backup_file"] = record.get("_backup_file")
    return lock, opened


def _sealed(record: dict, lock) -> dict:
    """``record`` sealed with its document's lock: in the clear only where
    the document goes and when it was kept, to tell a stale backup from a
    fresh one (classify_backup) before the password is asked for."""
    data = json.dumps(record, ensure_ascii=False).encode("utf-8")
    return {"version": BACKUP_VERSION, "format": SEALED, "locked": lock.seal_backup(data),
            **{key: record[key] for key in ("original_path", "saved_at", "source_mtime_ns")}}


def load_backup_content(editor, record: dict, *, modified: bool = True) -> None:
    """Put a backup record's content into ``editor``, with no undo history.

    Call is_restorable first. Set the editor's file path before calling:
    image names beside the original file resolve against it.
    """
    content = record.get("content", "")
    if record.get("version") is None:
        # Version 1 stored plain text. Restoring it as HTML would render
        # the user's angle brackets as markup.
        editor.setPlainText(content)
    else:
        editor.setHtml(content)
        normalize_after_set_html(editor.document())
        structure = record.get("structure")
        markdown = record.get("markdown")
        if isinstance(structure, dict):
            apply_structure(editor.document(), structure)
        elif isinstance(markdown, str) and markdown:
            _restore_older_markdown(editor.document(), markdown)
        if record.get("photo_groups") is True:
            # Kept as their paragraphs (HTML has no place for a group):
            # shown as the groups they were.
            photo_group_find.fold(editor.document())
    if record.get("markdown_source"):
        # Opened as its Markdown text: it is still saved as written.
        editor._markdown_source = True
    doc = editor.document()
    doc.clearUndoRedoStacks()
    # Set from the other state, so it holds: Qt remembers the undo step that
    # was saved, and setModified does nothing when the document already
    # says so. A backup the tidy-up changed (its fonts taken off) already
    # says modified, and clearing the stacks would bring back the step
    # setHtml saved, and with it "unmodified".
    doc.setModified(not modified)
    doc.setModified(modified)


def _text_print(doc) -> str:
    """A fingerprint of the document's characters, positions included."""
    return hashlib.sha256(doc.toRawText().encode("utf-8", "surrogatepass")).hexdigest()


def structure_of(doc) -> dict | None:
    """What an HTML snapshot cannot carry, by position: each paragraph's
    quote level and code block (fence and language), and the stretches of
    inline code. None when the document has none of them."""
    blocks = []
    code = []
    for number, block in enumerate(iter_blocks(doc)):
        fmt = block.blockFormat()
        kept = {}
        quote = rich_text.quote_depth(block)
        if quote:
            kept["quote"] = quote
        if rich_text.is_code_block(block):
            kept["fence"] = str(fmt.property(QTextFormat.Property.BlockCodeFence) or "")
            kept["language"] = str(fmt.property(QTextFormat.Property.BlockCodeLanguage) or "")
        if kept:
            blocks.append([number, kept])
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            it += 1
            if fragment.isValid() and fragment.charFormat().fontFixedPitch() \
                    and not fragment.charFormat().isImageFormat():
                start, end = fragment.position(), fragment.position() + fragment.length()
                if code and code[-1][1] == start:
                    code[-1][1] = end
                else:
                    code.append([start, end])
    if not blocks and not code:
        return None
    return {"text": _text_print(doc), "blocks": blocks, "code": code}


def _whole_number(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value < high


def apply_structure(doc, structure: dict) -> bool:
    """Put back what structure_of kept, over a document restored from the
    HTML of the same moment. Nothing is applied unless the text is the
    very text the list was taken from. True when it was applied."""
    blocks = structure.get("blocks")
    code = structure.get("code")
    if (structure.get("text") != _text_print(doc) or not isinstance(blocks, list)
            or not isinstance(code, list)):
        return False
    count, length = doc.blockCount(), doc.characterCount()
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for item in blocks:
        if not (isinstance(item, list) and len(item) == 2 and _whole_number(item[0], 0, count)
                and isinstance(item[1], dict)):
            continue
        block = doc.findBlockByNumber(item[0])
        kept = item[1]
        if _whole_number(kept.get("quote"), 1, 32):
            rich_text.set_quote_depth(block, kept["quote"])
        if "fence" in kept or "language" in kept:
            fmt = block.blockFormat()
            fmt.setProperty(QTextFormat.Property.BlockCodeFence, str(kept.get("fence") or "`"))
            fmt.setProperty(QTextFormat.Property.BlockCodeLanguage,
                            str(kept.get("language") or ""))
            QTextCursor(block).setBlockFormat(fmt)
    code_style = rich_text.style_format(rich_text.CODE, True)
    for item in code:
        if not (isinstance(item, list) and len(item) == 2 and _whole_number(item[0], 0, length)
                and _whole_number(item[1], item[0] + 1, length + 1)):
            continue
        piece = QTextCursor(doc)
        piece.setPosition(item[0])
        piece.setPosition(item[1], QTextCursor.MoveMode.KeepAnchor)
        piece.mergeCharFormat(code_style)
    cursor.endEditBlock()
    return True


def _restore_older_markdown(doc, markdown: str) -> None:
    """A record from a build that kept Markdown beside the HTML: its
    Markdown, but only when it reads back to exactly the HTML's text
    (typed Markdown characters would have changed it otherwise)."""
    if not holds_faithfully(markdown):
        return
    candidate = QTextDocument()
    read_markdown(candidate, markdown)
    # (The HTML holds a photo group as its paragraphs.)
    if photo_group.unfolded(candidate).toPlainText() == doc.toPlainText():
        read_markdown(doc, markdown)


def classify_backup(record: dict) -> str:
    """How a backup relates to the file it came from.

    Returns "untitled" (no original path), "fresh" (the backup is at
    least as new as the file, or the file is gone) or "stale" (the file
    on disk has moved on since the snapshot).

    A record with no ``source_mtime_ns`` cannot prove it is fresh, so an
    existing file makes it stale. Being wrong in that direction costs
    one Save As; being wrong the other way overwrites newer work.
    """
    path = record.get("original_path") if isinstance(record, dict) else None
    if not path:
        return "untitled"
    mtime = _source_mtime_ns(path)
    if mtime is None:
        return "fresh"
    saved = record.get("source_mtime_ns")
    if isinstance(saved, bool) or not isinstance(saved, int):
        return "stale"
    return "stale" if mtime > saved else "fresh"


class EditorBackup:
    """Manages the backup lifecycle for a single editor instance."""

    def __init__(self, editor, file_path: str | None, *, externalize=None):
        self._editor = editor
        self._file_path = file_path
        self._backup_id = _backup_id_for(file_path)
        # Maps a data: URI to a resolvable asset key. Injected so this
        # module stays testable without the asset layer running.
        self._externalize = externalize
        self._last_hash = ""
        self._written_revision = None
        self._oversize_until = 0.0
        # Silent while backups succeed; whoever shows the document hears of
        # a failure, and of the end of one (set_status_listener).
        self._problem = KEPT
        self._listener = None

        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(_DEBOUNCE_MS)
        self._timer.timeout.connect(self._on_timeout)

        self._max_timer = QTimer()
        self._max_timer.setInterval(_MAX_INTERVAL_MS)
        self._max_timer.timeout.connect(self._on_timeout)
        self._max_timer.start()

        self._editor.document().contentsChanged.connect(self._schedule)

    @property
    def problem(self) -> str:
        """KEPT while the backup is kept, else why it is not
        (WRITE_FAILED, TOO_LARGE)."""
        return self._problem

    def set_status_listener(self, listener) -> None:
        """Install ``listener(problem)``, called when the backup stops being
        kept and when it is kept again."""
        self._listener = listener

    def _report(self, problem: str) -> None:
        if problem != self._problem:
            self._problem = problem
            if self._listener is not None:
                self._listener(problem)

    @property
    def path(self) -> str:
        """Where this backup writes. Callers cleaning up an older file
        compare against it so they never delete the live snapshot."""
        return _backup_path_for(self._backup_id)

    def update_file_path(self, new_path: str) -> None:
        """Call when an untitled doc is saved with a new path for the first time."""
        old_backup = _backup_path_for(self._backup_id)
        self._file_path = new_path
        self._backup_id = _backup_id_for(new_path)
        self._written_revision = None          # the record names the path: write it again
        # Saving over the same path keeps the same ID, and removing that
        # file would leave the document with no crash protection.
        if old_backup != self.path and os.path.exists(old_backup):
            try:
                os.remove(old_backup)
            except OSError:
                pass

    def write_now(self) -> bool:
        """Force an immediate write, bypassing the debounce timer."""
        self._timer.stop()
        return self._write()

    def take_over(self, old_file: str) -> bool:
        """Protect the editor's restored content under this backup, then
        remove ``old_file``, the record that content came from.

        The replacement is written first: removing the old file before its
        successor exists is a window in which a second crash loses
        everything. A restored document that kept its path derives the
        same backup ID, so the replacement can be the old file itself, and
        then it stays. True when the content is safe on disk.
        """
        if not self.write_now():
            return False
        if os.path.abspath(old_file) != os.path.abspath(self.path):
            try:
                os.remove(old_file)
            except OSError:
                pass
        return True

    def release(self) -> None:
        """Stop writing but keep the file on disk.

        Used when MyEditor closes for an update: the next launch restores
        the tab from this file (workspace.py), so deleting it here would
        lose exactly the work the restart promised to keep.
        """
        self._timer.stop()
        self._max_timer.stop()
        self._listener = None
        try:
            self._editor.document().contentsChanged.disconnect(self._schedule)
        except RuntimeError:
            pass

    def delete(self) -> None:
        """Call on normal close: stop the timers and remove the backup file."""
        self.release()
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _schedule(self) -> None:
        self._timer.start()  # restarts the countdown on every change

    def _on_timeout(self) -> None:
        if time.monotonic() < self._oversize_until:
            return
        self._write()

    def _lock(self):
        """The document's lock when it is locked with a password, else None."""
        return getattr(self._editor, "_lock", None)

    def _snapshot(self, doc) -> str:
        """The HTML to persist of ``doc``, with big inline images
        externalized.

        Only the snapshot string is rewritten; the live document keeps
        whatever spelling it had, so nothing the user can see changes.
        """
        content = doc.toHtml()
        # A locked document's pictures stay inside its sealed record.
        if self._externalize is None or self._lock() is not None:
            return content

        def replace(match) -> str:
            uri = match.group(1)
            if len(uri) <= MIN_EXTERNALIZE_BYTES:
                return match.group(0)
            key = self._externalize(uri)
            return f'src="{key}"' if key else match.group(0)

        return _DATA_URI_SRC_RE.sub(replace, content)

    def _write(self) -> bool:
        """Write the snapshot. False means nothing usable is on disk yet."""
        doc = self._editor.document()
        if document_is_empty(doc):
            self._report(KEPT)                    # nothing there to lose
            return False
        # Nothing changed since the last write that is still on disk: an
        # idle tab costs nothing on the timer's ticks.
        revision = doc.revision()
        identity = tab_identity(self._editor)
        # Locking a document (or taking its lock off) writes its backup anew,
        # sealed (or not), though no word changed.
        locked = self._lock() is not None
        if (revision, identity, locked) == self._written_revision and os.path.exists(self.path):
            return True

        # A photo group is kept as its paragraphs, which HTML holds, and
        # folded again on restore (photo_groups).
        plain = photo_group.unfolded(doc)
        content = self._snapshot(plain)
        structure = structure_of(plain)
        # The fingerprint covers the structure and the tab's identity too:
        # a change only they show (a paragraph turned into a quote, a draft
        # saved under another identifier) is still written.
        content_key = (content + "\n" + json.dumps(structure, sort_keys=True)
                       + "\n" + json.dumps(identity, sort_keys=True) + f"\n{locked}")
        fingerprint = hashlib.sha256(
            f"{self._file_path or ''}\n{content_key}".encode("utf-8")
        ).hexdigest()
        # The fingerprint says the payload has not changed, not that the
        # file survived: a restore or a cleanup elsewhere can have
        # removed it, and skipping the write then would leave the
        # document unprotected until the next keystroke.
        if fingerprint == self._last_hash and os.path.exists(self.path):
            self._written_revision = (revision, identity, locked)
            return True  # already on disk, byte for byte

        record = {
            "version": BACKUP_VERSION,
            "format": "html",
            "original_path": self._file_path,
            "content": content,
            "assets": list(dict.fromkeys(_ASSET_KEY_RE.findall(content))),
            "saved_at": int(time.time()),
            # Changed by another app since it was read, or gone: the backup
            # can't prove it is newer than the file (classify_backup), so a
            # restore keeps the file and asks for a new name.
            "source_mtime_ns": (None if getattr(self._editor, "_changed_on_disk", False)
                                or getattr(self._editor, "_file_gone", False)
                                else _source_mtime_ns(self._file_path)),
        }
        if structure is not None:
            record["structure"] = structure
        if plain is not doc:
            record["photo_groups"] = True
        if getattr(self._editor, "_markdown_source", False):
            record["markdown_source"] = True
        record.update(identity)
        lock = self._lock()
        payload = json.dumps(_sealed(record, lock) if lock is not None else record,
                             ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_BACKUP_BYTES:
            self._oversize_until = time.monotonic() + _OVERSIZE_RETRY_S
            self._report(TOO_LARGE)
            return False

        # In one step: a crash while writing must leave the previous
        # backup whole, since that is exactly when it is needed.
        try:
            _ensure_backup_dir()
            write_text(self.path, payload)
        except OSError:
            # Never raised to the person: the status bar says it, quietly.
            self._report(WRITE_FAILED)
            return False
        self._last_hash = fingerprint
        self._written_revision = (revision, identity, locked)
        self._report(KEPT)
        return True


def read_backup(path: str) -> dict | None:
    """One backup record by file path, or None if it is missing or unreadable.

    The record carries ``_backup_file`` like the ones find_all_backups
    returns, so either can be handed to the same restore code.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    data["_backup_file"] = path
    return data


def find_all_backups() -> list[dict]:
    """Return every valid backup record found on disk."""
    folder = backup_dir()
    if not os.path.isdir(folder):
        return []
    results = []
    for name in os.listdir(folder):
        if not name.endswith(".autosave"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["_backup_file"] = path
            results.append(data)
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    return results
