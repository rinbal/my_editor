# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""When the articles published from this computer first went out.

NIP-23 makes ``published_at`` the time of an article's first
publication, and every later version keeps it. The relays that hold the
article are asked first, but they can be down, or the account's relay
list can have moved since; then the date this computer remembers keeps
an edit from being dated today. Kept per author and identifier in
``~/.config/my_editor/article_first_published.json``, the earliest date
known for each.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import user_dirs
from atomic_file import read_json, write_json

FILE_NAME = "article_first_published.json"


def default_path() -> Path:
    return user_dirs.config_dir() / FILE_NAME


class FirstPublications:
    """The earliest first publication date known here, per article."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else default_path()
        self._dates: Optional[Dict[str, int]] = None

    @staticmethod
    def _key(author: str, identifier: str) -> str:
        return f"{(author or '').lower()}:{identifier}"

    def _loaded(self) -> Dict[str, int]:
        if self._dates is None:
            articles = read_json(self._path, dict).get("articles")
            self._dates = {
                key: value for key, value in (articles.items() if isinstance(articles, dict)
                                              else ())
                if isinstance(key, str) and isinstance(value, int)
                and not isinstance(value, bool) and value > 0}
        return self._dates

    def get(self, author: str, identifier: str) -> Optional[int]:
        """When the article first went out, as far as this computer knows."""
        if not author or not identifier:
            return None
        return self._loaded().get(self._key(author, identifier))

    def remember(self, author: str, identifier: str, published_at: int) -> None:
        """Note that the article went out at ``published_at`` (an earlier
        date already known stays)."""
        if not author or not identifier or not published_at or published_at <= 0:
            return
        dates = self._loaded()
        key = self._key(author, identifier)
        if key in dates and dates[key] <= published_at:
            return
        dates[key] = int(published_at)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self._path, {"version": 1, "articles": dates})
