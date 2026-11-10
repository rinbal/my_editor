# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Why a text keeps its link name for its submissions (RG-M2r 10,
RG-M2r2a 1 and 2). The submission id comes from the link name (``d``):
another one would be another submission, so once a writer entry for it
is recorded the name stays.

:class:`SubmittedMarks` is what this computer holds of that, kept across
restarts: a mark per account and text, written when the entry is
recorded and when a receipt for it is held, in
``~/.config/my_editor/standup_submitted.json`` (no secret: the key is
the work's coordinate hash, the value whether the server stored it).

:class:`LinkNameLock` is the answer Article Details shows: what is held
of the text's submission (:class:`~.progress.Held`, whose words come
from :data:`~.progress.HELD_WORDS`, lesson 32), or that the list is
still being read, or could not be read: read-only meanwhile, never
guessed free. Pure: no Qt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

from atomic_file import read_json, write_json

from . import formats, words
from .progress import HELD_WORDS, Held

FILE_NAME = "standup_submitted.json"
# Far more than any writer's texts (a mark is about 90 bytes): a file
# larger than this is not parsed, and not written over.
MAX_FILE_BYTES = 4 * 1024 * 1024


def default_path() -> Path:
    return Path.home() / ".config" / "my_editor" / FILE_NAME


class SubmittedMarks:
    """Per account and text: a writer entry was recorded (``False``), and
    the server stored it (``True``). A mark only goes from not stored to
    stored; :meth:`forget` takes it back once no entry of the text is left
    (an entry discarded, a submission cancelled before it was sent)."""

    def __init__(self, path: Optional[Path] = None, *, kept: bool = True) -> None:
        """``kept``: in the file at ``path`` (the default one without);
        False holds the marks in memory only (a writer made for a test)."""
        self._path = (Path(path) if path is not None else default_path()) if kept else None
        self._marks: Optional[Dict[str, bool]] = None if kept else {}
        self._writable = kept

    @staticmethod
    def _key(writer: str, d: str) -> Optional[str]:
        writer = writer.lower() if isinstance(writer, str) else writer
        if not formats.is_hex64(writer) or not isinstance(d, str) or not d:
            return None
        return formats.coordinate_hash(writer, d)

    def _loaded(self) -> Dict[str, bool]:
        if self._marks is None:
            try:
                size = os.path.getsize(self._path)
            except OSError:
                size = 0
            if size > MAX_FILE_BYTES:
                # Never parsed, never written over: the list read decides.
                self._marks, self._writable = {}, False
                return self._marks
            texts = read_json(self._path, dict).get("texts")
            self._marks = {key: value for key, value in (texts.items() if isinstance(texts, dict)
                                                          else ())
                           if formats.is_hex64(key) and isinstance(value, bool)}
        return self._marks

    def get(self, writer: str, d: str) -> Optional[bool]:
        """None without a mark; else whether the server stored it."""
        key = self._key(writer, d)
        return None if key is None else self._loaded().get(key)

    def remember(self, writer: str, d: str, *, stored: bool) -> None:
        key = self._key(writer, d)
        marks = self._loaded()
        if key is None or marks.get(key) is True or marks.get(key) is bool(stored):
            return
        marks[key] = bool(stored)
        self._write()

    def forget(self, writer: str, d: str) -> None:
        key = self._key(writer, d)
        if key is not None and self._loaded().pop(key, None) is not None:
            self._write()

    def _write(self) -> None:
        if not self._writable:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._path, {"version": 1, "texts": dict(sorted(self._marks.items()))})


@dataclass(frozen=True)
class LinkNameLock:
    """Whether a text's link name is fixed for its submissions: ``held``,
    what the server holds of it as far as this computer knows (a run's
    ``Progress.held``; an entry or a mark, STORED once a receipt was held,
    else NOTHING); ``checking``, the list of submissions is being read;
    ``unchecked``, it could not be read whole (Check Again reads it)."""

    held: Optional[Held] = None
    checking: bool = False
    unchecked: bool = False

    @property
    def locked(self) -> bool:
        return self.held is not None or self.checking or self.unchecked

    def words(self) -> Tuple[str, str]:
        """(the note, its tooltip), untranslated; empty while it is free."""
        if self.held is not None:
            said = HELD_WORDS[self.held]
            return said.link_name, said.link_name_why
        if self.checking:
            return words.LINK_NAME_CHECKING, words.LINK_NAME_CHECKING_WHY
        if self.unchecked:
            return words.LINK_NAME_NOT_CHECKED, words.LINK_NAME_CHECKING_WHY
        return "", ""


def held_of(stored: bool) -> Held:
    """What an entry or a mark says is held: a receipt, or not known."""
    return Held.STORED if stored else Held.NOTHING


__all__ = ["FILE_NAME", "LinkNameLock", "MAX_FILE_BYTES", "SubmittedMarks", "default_path",
           "held_of"]
