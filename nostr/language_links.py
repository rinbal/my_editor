# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The private links between an article's language versions, kept here.

A linked document's link (multilingual_authoring.private_link: its group,
its versions, its reference, where the versions are saved) goes with its
tab (the restart and crash records) and, encrypted, with its draft. A
Markdown file holds none of it: people share their files and commit them
to their blog's repository, and the link names this computer's folders,
the account and the original's text. So a saved document's link is kept
on this computer instead, per account and article identifier, in
``~/.config/my_editor/language_links.json``, readable by this computer's
account only (atomic_file), as the record of published articles is
(publication_journal). Opening the file again finds it there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import user_dirs
from atomic_file import read_json, write_json

FILE_NAME = "language_links.json"


def default_path() -> Path:
    return user_dirs.config_dir() / FILE_NAME


class LanguageLinks:
    """The link of each linked document saved here, per article."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else default_path()
        self._links: Optional[Dict[str, dict]] = None

    @staticmethod
    def _key(owner: str, identifier: str) -> str:
        return f"{(owner or '').lower()}:{identifier}"

    def _loaded(self) -> Dict[str, dict]:
        if self._links is None:
            links = read_json(self._path, dict).get("links")
            self._links = {key: value for key, value in (links.items() if isinstance(links, dict)
                                                         else ())
                           if isinstance(key, str) and isinstance(value, dict)}
        return self._links

    def get(self, owner: str, identifier: str) -> Optional[dict]:
        """The link kept for the account's article ``identifier``, or None."""
        if not owner or not identifier:
            return None
        found = self._loaded().get(self._key(owner, identifier))
        return dict(found) if found is not None else None

    def keep(self, owner: str, identifier: str, record: dict) -> None:
        """Keep ``record`` as the link of the account's article ``identifier``."""
        if not owner or not identifier or not isinstance(record, dict):
            return
        links = self._loaded()
        key = self._key(owner, identifier)
        if links.get(key) == record:
            return
        links[key] = record
        write_json(self._path, {"version": 1, "links": links}, ensure_ascii=False)
