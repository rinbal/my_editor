# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An approval a signer app asks for in a browser (46.md ``auth_url``).

A signer may answer a request with an address where the person approves
it first; the real answer follows under the same request. While pairing,
the Connect window opens it and says so (:mod:`.connect_dialog`). After
pairing, any request of an account's signer can be asked this way
(``BunkerSessionPool.auth_challenge``, audit F8): :class:`SignerApprovals`
opens each address once, when it can be opened safely, and says it once,
calmly, in the status bar. One that cannot is not opened; its request
ends as one the signer did not answer.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional, Set

from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QDesktopServices

from i18n import N_, _
from url_safety import is_safe_external_url

log = logging.getLogger(__name__)

CONFIRM_IN_SIGNER = N_("Confirm in your signer app")


def open_approval(url: str, *, open_url: Optional[Callable[[QUrl], object]] = None) -> bool:
    """Open the address a signer asked to approve at, when it can be
    opened safely (http(s) only, nothing local); whether it was."""
    if not is_safe_external_url(url):
        return False
    (open_url or QDesktopServices.openUrl)(QUrl(url))
    return True


class SignerApprovals(QObject):
    """``asked(url)`` for every approval a paired signer asks for;
    ``say(text)`` is the status bar."""

    def __init__(self, say: Callable[[str], None], *,
                 open_url: Optional[Callable[[QUrl], object]] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._say, self._open_url = say, open_url
        self._seen: Set[str] = set()

    def asked(self, url: str) -> None:
        if url in self._seen:
            return                  # one request asked again, or the next: once
        self._seen.add(url)
        if open_approval(url, open_url=self._open_url):
            self._say(_(CONFIRM_IN_SIGNER))
        else:
            log.info("a signer asked for approval at an address not opened")


__all__ = ["CONFIRM_IN_SIGNER", "SignerApprovals", "open_approval"]
