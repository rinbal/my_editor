# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Removing a published work's encrypted files (plan 9.6; M-4 M14), as
the web app does (``services/editorial/blob-cleanup.js``, feat/redaktion-f5a).

Until publication the pictures of a submission lie encrypted on the
writer's media servers; once the work is public its plain files are on
the writer's servers, and the encrypted copies go. Pure: what to delete,
and the record of what is done and when to try again.

- What goes: every file the verified revisions up to the published one
  name, except one a later revision names too (a correction that still
  waits may show it, and the editorial team must read it), and except
  one another submission of this account names: only the last user of a
  file removes it.
- Where: the servers each verified revision's signed blob list names for
  the file, and the writer's own media servers.
- The record (per account, in the app's settings; hashes and server
  addresses only, which the servers hold anyway, nothing secret): each
  success is kept, so nothing is asked twice; a server that failed is
  asked again after a growing pause, for at most seven days from the
  first try. After that the rest is let be.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import formats

# The pauses between tries, in seconds; the last one repeats.
PAUSES = (5 * 60, 30 * 60, 2 * 3600, 6 * 3600, 24 * 3600)
# How long a cleanup keeps trying, from its first try.
GIVES_UP = 7 * 24 * 3600
SETTINGS_KEY = "standup_blob_cleanups"

Pair = Tuple[str, str]                    # (cipher sha256, server origin)


def to_delete(records: Sequence[Tuple[int, Sequence[Tuple[str, Sequence[str]]]]],
              published: int, servers: Iterable[str],
              named_elsewhere: Iterable[str] = ()) -> List[Pair]:
    """The ``(sha256, server)`` to delete for a work published as revision
    ``published``. ``records`` are ``(revision, blobs)`` of the verified
    revisions, each blob ``(sha256, servers)`` as its signed body names
    it; ``servers`` the writer's own media servers; ``named_elsewhere``
    the files another submission of this account names."""
    keep = set(named_elsewhere)
    keep.update(sha for revision, blobs in records if revision > published for sha, _s in blobs)
    further = [s for s in (_origin(server) for server in servers) if s]
    pairs: Dict[Pair, None] = {}
    for revision, blobs in records:
        if revision > published:
            continue
        for sha, named in blobs:
            if sha in keep:
                continue
            for server in dict.fromkeys([*(_origin(s) for s in named), *further]):
                if server:
                    pairs[(sha, server)] = None
    return list(pairs)


def _origin(server: str) -> str:
    return server.rstrip("/") if isinstance(server, str) and server.startswith(
        ("https://", "http://")) else ""


def _key(pair: Pair) -> str:
    return f"{pair[0]} {pair[1]}"


def due(entry: Optional[Mapping[str, Any]], now: float) -> bool:
    """Whether a cleanup with ``entry`` (or none yet) runs at ``now``:
    not over, and its pause has passed."""
    return entry is None or (not entry.get("over") and now >= entry.get("next", 0))


def left(entry: Optional[Mapping[str, Any]], pairs: Sequence[Pair]) -> List[Pair]:
    """What is left of ``pairs`` after what ``entry`` records as done."""
    done = set((entry or {}).get("done") or ())
    return [pair for pair in pairs if _key(pair) not in done]


def after_try(entry: Optional[Mapping[str, Any]], pairs: Sequence[Pair],
              gone: Iterable[Pair], now: float) -> Dict[str, Any]:
    """The record after a try at ``now``: every pair gone is kept as done;
    with nothing left it is over, as it is seven days after the first
    try; else the next try comes after the next pause."""
    entry = entry or {}
    started = entry.get("started", now)
    tries = entry.get("tries", 0) + 1
    done = list(dict.fromkeys([*(entry.get("done") or ()), *(_key(p) for p in gone)]))
    rest = any(_key(pair) not in done for pair in pairs)
    pause = PAUSES[min(tries, len(PAUSES)) - 1]
    return {"started": started, "tries": tries, "done": done, "next": now + pause,
            "over": not rest or now + pause > started + GIVES_UP}


class Cleanups:
    """The cleanups of one account, by sid, in the app's settings (key
    :data:`SETTINGS_KEY`, then the account's pubkey). ``load`` and
    ``save`` are seams."""

    def __init__(self, pubkey: str, *, load: Optional[Callable[[], dict]] = None,
                 save: Optional[Callable[[str, Any], None]] = None) -> None:
        if load is None or save is None:
            import settings as app_settings
            load = load or app_settings.load_settings
            save = save or app_settings.save_setting
        self._pubkey, self._load, self._save = pubkey, load, save

    def _all(self) -> Dict[str, Any]:
        stored = (self._load() or {}).get(SETTINGS_KEY)
        return dict(stored) if isinstance(stored, dict) else {}

    def entry(self, sid: str) -> Optional[Dict[str, Any]]:
        mine = self._all().get(self._pubkey)
        entry = mine.get(sid) if isinstance(mine, dict) else None
        return entry if isinstance(entry, dict) and formats.is_int(entry.get("tries")) else None

    def keep(self, sid: str, entry: Mapping[str, Any]) -> None:
        stored = self._all()
        mine = stored.get(self._pubkey)
        mine = dict(mine) if isinstance(mine, dict) else {}
        mine[sid] = dict(entry)
        stored[self._pubkey] = mine
        self._save(SETTINGS_KEY, stored)


__all__ = ["Cleanups", "GIVES_UP", "PAUSES", "Pair", "SETTINGS_KEY", "after_try", "due",
           "left", "to_delete"]
