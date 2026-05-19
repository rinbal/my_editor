"""Persistent Blossom-server preferences.

One JSON file in ~/.config/my_editor/blossom_servers.json. Atomic
write (temp file + rename in the same directory) so a crash mid-write
can't corrupt the store — same pattern as ``ProfileStore``.

An empty / missing ``custom`` list means "use the bundled defaults".
The user's primary is always ``custom[0]`` when ``custom`` is non-empty.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .servers import DEFAULT_BLOSSOM_SERVERS


SETTINGS_DIR = Path.home() / ".config" / "my_editor"
SETTINGS_FILE = SETTINGS_DIR / "blossom_servers.json"


def _normalize(url: str) -> Optional[str]:
    """Normalize a server URL to ``scheme://host[:port]`` form, lowercase
    host, no trailing slash, no path. Returns None for anything that
    isn't a usable Blossom origin (we accept https:// and http://; the
    latter only so localhost dev servers work for testing)."""
    if not isinstance(url, str):
        return None
    raw = url.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except (ValueError, AttributeError):
        return None
    if parsed.scheme not in ("https", "http"):
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


class BlossomSettings:
    """In-memory view over ``blossom_servers.json`` with explicit save."""

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self._custom: List[str] = []
        self._load()

    # -- read --------------------------------------------------------------

    @property
    def custom_servers(self) -> List[str]:
        """User-curated server list. Empty means "use defaults"."""
        return list(self._custom)

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
        cleaned: List[str] = []
        seen: set[str] = set()
        for url in servers:
            normalized = _normalize(url)
            if normalized is None or normalized in seen:
                continue
            cleaned.append(normalized)
            seen.add(normalized)
        self._custom = cleaned
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
        first if the list was empty, then removes — so users can prune
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
        """Forget the custom list — ``configured_servers`` will return
        ``DEFAULT_BLOSSOM_SERVERS`` again."""
        self._custom = []
        self._save()
        return list(self.configured_servers)

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
        cleaned: List[str] = []
        seen: set[str] = set()
        for entry in raw_custom:
            normalized = _normalize(entry) if isinstance(entry, str) else None
            if normalized is None or normalized in seen:
                continue
            cleaned.append(normalized)
            seen.add(normalized)
        self._custom = cleaned

    def _save(self) -> None:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(SETTINGS_DIR, 0o700)
        except OSError:
            pass

        payload = {"version": 1, "custom": list(self._custom)}

        fd, tmp_path = tempfile.mkstemp(
            prefix=".blossom_servers_", suffix=".json.tmp", dir=str(SETTINGS_DIR)
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
