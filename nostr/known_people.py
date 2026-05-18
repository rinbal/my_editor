"""Disk-backed cache of people we know about, keyed by hex pubkey.

Populated from three sources, recorded so the picker can show provenance:

  ``contact``  the user's NIP-02 follow list (kind 3)
  ``search``   results streamed back from NIP-50 search relays
  ``mention``  manually-pasted nostr:n… URI in a publish dialog

Persisted to ``~/.config/my_editor/known_people.json`` with atomic
rewrites so a crash mid-write can't corrupt the cache.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


KNOWN_PEOPLE_FILE = Path.home() / ".config" / "my_editor" / "known_people.json"


@dataclass
class Person:
    """Display data for one Nostr pubkey, sufficient to render in the picker."""

    pubkey: str                         # hex, 64 chars
    display_name: str = ""              # display_name or name from kind 0; or petname from kind 3
    picture: str = ""                   # avatar URL
    nip05: str = ""                     # NIP-05 identifier (alice@example.com)
    relay_hint: str = ""                # best-known relay for them
    source: str = ""                    # "contact" / "search" / "mention" — provenance
    updated_at: int = 0                 # unix seconds — when metadata last refreshed

    def search_haystack(self) -> str:
        """Lowercased blob the picker matches against."""
        return f"{self.display_name}\n{self.nip05}".lower()


# --------------------------------------------------------------------------- #
# Store                                                                        #
# --------------------------------------------------------------------------- #

class KnownPeople:
    """JSON-backed dict of Person, keyed by hex pubkey.

    All reads are served from an in-memory copy populated on construction;
    mutation methods persist immediately. ``search`` does a cheap local
    substring scan suitable for the typical hundreds-to-low-thousands of
    contacts a user will accumulate.
    """

    def __init__(self, path: Path = KNOWN_PEOPLE_FILE) -> None:
        self._path = path
        self._people: dict[str, Person] = {}
        self._load()

    # -- read --------------------------------------------------------------

    def get(self, pubkey_hex: str) -> Optional[Person]:
        return self._people.get(pubkey_hex)

    def all(self) -> List[Person]:
        return list(self._people.values())

    def __len__(self) -> int:
        return len(self._people)

    def __contains__(self, pubkey_hex: str) -> bool:
        return pubkey_hex in self._people

    def search(self, query: str, limit: int = 10) -> List[Person]:
        """Case-insensitive substring search across display_name and nip05.

        Ranking: prefix matches first (display_name → nip05), then any
        substring matches, both sorted alphabetically by display_name within
        each tier. Empty query returns the most-recently-updated entries.
        """
        q = query.strip().lower()
        if not q:
            return sorted(
                self._people.values(),
                key=lambda p: p.updated_at,
                reverse=True,
            )[:limit]

        prefix: list[Person] = []
        contains: list[Person] = []
        for person in self._people.values():
            name = person.display_name.lower()
            nip05 = person.nip05.lower()
            if name.startswith(q) or nip05.startswith(q):
                prefix.append(person)
            elif q in person.search_haystack():
                contains.append(person)

        prefix.sort(key=lambda p: p.display_name.lower())
        contains.sort(key=lambda p: p.display_name.lower())
        return (prefix + contains)[:limit]

    # -- mutate ------------------------------------------------------------

    def upsert(self, person: Person, *, defer_save: bool = False) -> Person:
        """Insert or merge by pubkey.

        Merging rules: any non-empty field on the new record wins; empty
        fields preserve whatever we had. ``source`` follows the same rule
        (so a later ``contact`` overrides an earlier ``mention``).
        """
        existing = self._people.get(person.pubkey)
        if existing is None:
            self._people[person.pubkey] = person
        else:
            merged = Person(
                pubkey=person.pubkey,
                display_name=person.display_name or existing.display_name,
                picture=person.picture or existing.picture,
                nip05=person.nip05 or existing.nip05,
                relay_hint=person.relay_hint or existing.relay_hint,
                source=person.source or existing.source,
                updated_at=max(person.updated_at, existing.updated_at),
            )
            self._people[person.pubkey] = merged
        if not defer_save:
            self._save()
        return self._people[person.pubkey]

    def upsert_many(self, people: Iterable[Person]) -> None:
        """Bulk version — single disk write at the end."""
        for p in people:
            self.upsert(p, defer_save=True)
        self._save()

    def remove(self, pubkey_hex: str) -> bool:
        if pubkey_hex not in self._people:
            return False
        del self._people[pubkey_hex]
        self._save()
        return True

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        for entry in data.get("people", []):
            try:
                p = Person(**entry)
            except TypeError:
                continue  # forward-compat: skip entries we don't understand
            if isinstance(p.pubkey, str) and len(p.pubkey) == 64:
                self._people[p.pubkey] = p

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"people": [asdict(p) for p in self._people.values()]}
        fd, tmp_path = tempfile.mkstemp(
            prefix=".known_people_", suffix=".json.tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
