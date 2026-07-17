#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
from dataclasses import dataclass

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

from constants import APP_REPO_SLUG, APP_RELEASES_URL, APP_VERSION

_API_URL = f"https://api.github.com/repos/{APP_REPO_SLUG}/releases/latest"
_TIMEOUT_MS = 10000


@dataclass(frozen=True)
class Asset:
    """One downloadable file attached to a GitHub release."""
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class ReleaseInfo:
    """The latest release: its version, its web page, and its download assets."""
    version: str
    page_url: str
    assets: tuple  # tuple[Asset, ...]


def _version_tuple(version: str):
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
        release_url = data.get("html_url") or APP_RELEASES_URL

        latest_tuple = _version_tuple(latest)
        current_tuple = _version_tuple(APP_VERSION)
        if latest_tuple is None or current_tuple is None:
            self.failed.emit("Could not parse version numbers.")
            return

        if latest_tuple > current_tuple:
            assets = tuple(
                Asset(
                    name=a.get("name", ""),
                    url=a.get("browser_download_url", ""),
                    size=a.get("size", 0) or 0,
                )
                for a in data.get("assets", [])
                if a.get("browser_download_url")
            )
            self.update_available.emit(ReleaseInfo(latest, release_url, assets))
        else:
            self.up_to_date.emit()
