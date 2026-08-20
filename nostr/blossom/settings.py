# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Persistent Blossom-server preferences.

One JSON file in ~/.config/my_editor/blossom_servers.json. Atomic
write (temp file + rename in the same directory) so a crash mid-write
can't corrupt the store, the same pattern as ``ProfileStore``.

An empty / missing ``custom`` list means "use the bundled defaults".
The user's primary is always ``custom[0]`` when ``custom`` is non-empty.

``custom`` is the user's explicit choice and stays authoritative for
uploads (ADR AD-13.1). A server list discovered from the network is
recorded beside it, never inside it: ``discovered`` and
``discovered_pubkey`` are derived, disposable and used for retrieval and
suggestions, while ``config_origin`` records who decided the list that
is in force. The guard that stops a discovery overwriting a choice lives
inside :meth:`BlossomSettings.adopt_discovered`, not in its callers, so a
caller cannot get it wrong.

Format version stays 1: the three new keys are optional and derived, so
an older build that rewrites this file simply drops them and costs one
re-fetch (AD-10).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional

import url_safety

from .servers import DEFAULT_BLOSSOM_SERVERS


SETTINGS_DIR = Path.home() / ".config" / "my_editor"
SETTINGS_FILE = SETTINGS_DIR / "blossom_servers.json"

# Who decided the list currently in force. "user" is an explicit choice,
# "bud03" is a list adopted from the profile's published kind 10063, and
# "default" is a deliberate revert to the bundled servers. The empty
# string means nothing has been decided yet, which is the only state a
# discovered list may be adopted into.
CONFIG_ORIGIN_UNSET = ""
CONFIG_ORIGIN_USER = "user"
CONFIG_ORIGIN_BUD03 = "bud03"
CONFIG_ORIGIN_DEFAULT = "default"

_CONFIG_ORIGINS = (
    CONFIG_ORIGIN_UNSET,
    CONFIG_ORIGIN_USER,
    CONFIG_ORIGIN_BUD03,
    CONFIG_ORIGIN_DEFAULT,
)

# Matches the BUD-03 parsing cap in ``server_list``. Stated here rather
# than imported because that module reads this one, and a settings file
# must not depend on the discovery code to load.
_MAX_DISCOVERED = 10


def _normalize(url: str) -> Optional[str]:
    """Normalize a server URL to ``scheme://host[:port]`` form, lowercase
    host, no trailing slash, no path. Returns None for anything that
    isn't a usable Blossom origin: https anywhere, http only on
    loopback, which is what the docstring above always promised and the
    old scheme check did not enforce. ``_load`` re-normalizes every
    persisted entry, so a hand-edited file naming a plain-http host is
    dropped on read as well as on write."""
    if not isinstance(url, str):
        return None
    origin = url_safety.origin_of(url.strip())
    if origin is None or not url_safety.is_safe_media_url(origin):
        return None
    return origin


def _clean(servers) -> List[str]:
    """Normalize a sequence of server URLs, dropping refusals and dupes."""
    cleaned: List[str] = []
    seen: set[str] = set()
    if not isinstance(servers, (list, tuple)):
        return cleaned
    for url in servers:
        normalized = _normalize(url)
        if normalized is None or normalized in seen:
            continue
        cleaned.append(normalized)
        seen.add(normalized)
    return cleaned


class BlossomSettings:
    """In-memory view over ``blossom_servers.json`` with explicit save."""

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self._custom: List[str] = []
        self._discovered: List[str] = []
        self._discovered_pubkey: str = ""
        self._config_origin: str = CONFIG_ORIGIN_UNSET
        self._load()

    # -- read --------------------------------------------------------------

    @property
    def custom_servers(self) -> List[str]:
        """User-curated server list. Empty means "use defaults"."""
        return list(self._custom)

    @property
    def has_explicit_config(self) -> bool:
        """Whether the user has chosen servers themselves.

        The single question every discovery path asks before it does
        anything: while this is True the user's list wins, for uploads
        and for everything else.
        """
        return bool(self._custom)

    @property
    def config_origin(self) -> str:
        """One of the ``CONFIG_ORIGIN_*`` values. Derived, not a choice."""
        return self._config_origin

    @property
    def discovered_servers(self) -> List[str]:
        """The last published kind 10063 seen for ``discovered_pubkey``.

        Never part of ``configured_servers``: a discovered list is for
        retrieval and for offering (AD-13.2, AD-13.3). It only becomes an
        upload target through :meth:`adopt_discovered`, which refuses
        unless there is nothing to overwrite.
        """
        return list(self._discovered)

    @property
    def discovered_pubkey(self) -> str:
        return self._discovered_pubkey

    @property
    def configured_servers(self) -> List[str]:
        """The list to actually use: custom if set, else the defaults.

        Index 0 is the primary; the rest are mirror targets.
        """
        return list(self._custom) if self._custom else list(DEFAULT_BLOSSOM_SERVERS)

    @property
    def primary(self) -> str:
        """Convenience: first entry of ``configured_servers``."""
        return self.configured_servers[0]

    # -- mutate ------------------------------------------------------------

    def set_custom_servers(self, servers: List[str]) -> List[str]:
        """Replace the custom list. Returns the normalized, deduplicated
        list actually persisted (so callers can update their UI from the
        canonical view).

        An empty list reverts to "use defaults".
        """
        cleaned = _clean(servers)
        self._custom = cleaned
        # Emptying the list is a revert, and a revert must not be undone
        # by the next discovery refresh. Recording it as "default" is
        # what makes ``adopt_discovered`` refuse afterwards.
        self._config_origin = (
            CONFIG_ORIGIN_USER if cleaned else CONFIG_ORIGIN_DEFAULT
        )
        self._save()
        return list(self._custom)

    def add_server(self, url: str) -> List[str]:
        """Append a server to the custom list. If the list was empty we
        first materialize the defaults so the user keeps everything they
        had plus the new one. No-op if ``url`` is already present.
        """
        normalized = _normalize(url)
        if normalized is None:
            return list(self._custom)
        base = self._custom if self._custom else list(DEFAULT_BLOSSOM_SERVERS)
        if normalized in base:
            return list(base)
        base.append(normalized)
        return self.set_custom_servers(base)

    def remove_server(self, url: str) -> List[str]:
        """Drop a server from the custom list. Materializes defaults
        first if the list was empty, then removes, so users can prune
        a default they don't want."""
        normalized = _normalize(url)
        if normalized is None:
            return list(self._custom)
        base = self._custom if self._custom else list(DEFAULT_BLOSSOM_SERVERS)
        if normalized not in base:
            return list(base)
        base = [s for s in base if s != normalized]
        return self.set_custom_servers(base)

    def make_primary(self, url: str) -> List[str]:
        """Move ``url`` to index 0 of the custom list. Materializes
        defaults if needed. No-op if the URL isn't in the list."""
        normalized = _normalize(url)
        if normalized is None:
            return list(self._custom)
        base = self._custom if self._custom else list(DEFAULT_BLOSSOM_SERVERS)
        if normalized not in base:
            return list(base)
        base = [normalized] + [s for s in base if s != normalized]
        return self.set_custom_servers(base)

    def reset_to_defaults(self) -> List[str]:
        """Forget the custom list. ``configured_servers`` returns
        ``DEFAULT_BLOSSOM_SERVERS`` again.

        Reverting is a decision, so it is recorded as one: a user who
        goes back to the bundled servers is not silently re-adopted into
        their published list on the next refresh.
        """
        self._custom = []
        self._config_origin = CONFIG_ORIGIN_DEFAULT
        self._save()
        return list(self.configured_servers)

    # -- discovery ---------------------------------------------------------

    def record_discovered(self, servers: List[str], pubkey_hex: str) -> None:
        """Remember a published kind 10063 without acting on it.

        Writes the derived keys and nothing else. ``configured_servers``
        cannot change here, whatever the caller passes: a discovered list
        is retrieval and suggestion material until the user says
        otherwise (AD-13.2, AD-13.3).
        """
        self._discovered = _clean(servers)[:_MAX_DISCOVERED]
        self._discovered_pubkey = str(pubkey_hex or "")
        self._save()

    def adopt_discovered(self, servers: List[str], pubkey_hex: str) -> List[str]:
        """Adopt a published list as the user's own, or refuse.

        Only the first run qualifies: there must be no custom list AND no
        record of the user having decided anything, which is what
        ``config_origin`` carries. Anything else returns ``[]`` and
        changes nothing at all.

        The guard is here rather than in the caller on purpose (AD-13.1).
        A buggy or over-eager caller must not be able to overwrite a
        user's servers, and there is exactly one way into ``_custom``
        from the network: this method.
        """
        if self._custom or self._config_origin != CONFIG_ORIGIN_UNSET:
            return []
        cleaned = _clean(servers)[:_MAX_DISCOVERED]
        if not cleaned:
            return []
        self._custom = cleaned
        self._discovered = list(cleaned)
        self._discovered_pubkey = str(pubkey_hex or "")
        self._config_origin = CONFIG_ORIGIN_BUD03
        self._save()
        return list(self._custom)

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
        raw_custom = data.get("custom", [])
        if not isinstance(raw_custom, list):
            return
        self._custom = _clean(raw_custom)

        # Everything below is derived and disposable, so a wrong type, a
        # missing key or an unreadable value costs a re-fetch and never a
        # crash (AD-10). Losing it must never change what gets uploaded.
        self._discovered = _clean(data.get("discovered"))[:_MAX_DISCOVERED]
        raw_pubkey = data.get("discovered_pubkey")
        self._discovered_pubkey = raw_pubkey if isinstance(raw_pubkey, str) else ""

        raw_origin = data.get("config_origin")
        if isinstance(raw_origin, str) and raw_origin in _CONFIG_ORIGINS:
            self._config_origin = raw_origin
        else:
            # Written by a build that predates this key, or corrupt. A
            # non-empty list is an explicit choice; an empty one in a
            # file that exists at all means the user reverted, and a
            # revert must not be undone by the next discovery.
            self._config_origin = (
                CONFIG_ORIGIN_USER if self._custom else CONFIG_ORIGIN_DEFAULT
            )

    def _save(self) -> None:
        # The temp file goes beside the target, not beside the default
        # settings file: os.replace cannot cross filesystems, and a
        # caller that passed its own path must not have writes land in
        # the real config directory.
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass

        # Version stays 1. The three keys below are additive and derived,
        # so an older build that drops them loses nothing but a fetch.
        payload = {
            "version": 1,
            "custom": list(self._custom),
            "config_origin": self._config_origin,
            "discovered": list(self._discovered),
            "discovered_pubkey": self._discovered_pubkey,
        }

        fd, tmp_path = tempfile.mkstemp(
            prefix=".blossom_servers_", suffix=".json.tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self._path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
