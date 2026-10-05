#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import re
from dataclasses import dataclass

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from constants import APP_REPO_SLUG, APP_RELEASES_URL, APP_VERSION
from url_safety import is_safe_external_url

_API_URL = f"https://api.github.com/repos/{APP_REPO_SLUG}/releases/latest"
_TIMEOUT_MS = 10000

# Release notes are a page of Markdown. Anything longer is cut, not shown whole.
_MAX_NOTES_CHARS = 64 * 1024

_SHA256 = re.compile(r"\Asha256:([0-9a-f]{64})\Z", re.IGNORECASE)


@dataclass(frozen=True)
class Asset:
    """One downloadable file attached to a GitHub release.

    ``sha256`` is the hash GitHub computed when the file was uploaded, as
    lowercase hex, or "" when the release does not say. The updater only
    installs a file it can check against it.
    """
    name: str
    url: str
    size: int
    sha256: str = ""


@dataclass(frozen=True)
class ReleaseInfo:
    """The latest release: its version, its web page, its download assets,
    and its notes (Markdown, possibly empty)."""
    version: str
    page_url: str
    assets: tuple  # tuple[Asset, ...]
    notes: str = ""


def parse_sha256(digest) -> str:
    """The hex hash from a GitHub asset ``digest`` ("sha256:<hex>"), or ""."""
    match = _SHA256.match(digest) if isinstance(digest, str) else None
    return match.group(1).lower() if match else ""


def version_tuple(version: str):
    """Parse a "1.2.3" style string into a tuple of ints, or None if it
    does not look like a version number."""
    try:
        return tuple(int(part) for part in version.strip().split("."))
    except (ValueError, AttributeError):
        return None


class UpdateChecker(QObject):
    """Checks the GitHub releases API for a version newer than the running one."""

    update_available = Signal(object)     # ReleaseInfo
    up_to_date = Signal()
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)

    def check(self):
        request = QNetworkRequest(QUrl(_API_URL))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setTransferTimeout(_TIMEOUT_MS)
        reply = self._manager.get(request)
        reply.finished.connect(lambda: self._on_finished(reply))

    def _on_finished(self, reply: QNetworkReply):
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self.failed.emit(reply.errorString())
            return

        try:
            data = json.loads(bytes(reply.readAll()).decode("utf-8"))
            tag = data.get("tag_name", "")
        except Exception as e:
            self.failed.emit(str(e))
            return

        latest = tag[1:] if tag.startswith("v") else tag
        # Validate here rather than at the openUrl call sites: a release
        # page URL is JSON from the network, and there are three of them.
        html_url = data.get("html_url") or ""
        release_url = html_url if is_safe_external_url(html_url) else APP_RELEASES_URL

        latest_tuple = version_tuple(latest)
        current_tuple = version_tuple(APP_VERSION)
        if latest_tuple is None or current_tuple is None:
            self.failed.emit("Could not parse version numbers.")
            return

        if latest_tuple > current_tuple:
            assets = tuple(
                Asset(
                    name=a.get("name", ""),
                    url=a.get("browser_download_url", ""),
                    size=a.get("size", 0) or 0,
                    sha256=parse_sha256(a.get("digest")),
                )
                for a in data.get("assets", [])
                if is_safe_external_url(a.get("browser_download_url") or "")
            )
            notes = data.get("body")
            notes = notes[:_MAX_NOTES_CHARS] if isinstance(notes, str) else ""
            self.update_available.emit(ReleaseInfo(latest, release_url, assets, notes))
        else:
            self.up_to_date.emit()
