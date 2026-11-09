# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading what a signer connection's relays carry (STANDUP's editorial
contract 12.5 "Relay content caps").

A connection to a signer app pasted as a ``bunker://`` address uses the
signer's relays, and some cap the content of the events that carry its
requests and answers. Each relay's NIP-11 document says so; this reads
it once per connection, bounded in size (64 KiB) and time (4 seconds),
over https for wss:// (http for ws://), without cookies, and without
following a redirect, and notes what it states in
``nostr/signer_limits.py``, the one place every bound is taken from. A
document that cannot be read counts as stating no cap there (except for
the relays the contract names), as the web app does.

The reader says when the bound of a connection is known
(:meth:`RelayCapsReader.read`'s ``done``): as soon as one relay states no
cap, or when every one has answered or given up. The session pool waits
for that before it hands out a signer, so the first request is measured
against the relays it travels over.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable, List, Optional, Set

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from . import signer_limits

NIP11_TIMEOUT_MS = 4_000
NIP11_MAX_BYTES = 64 * 1024

_USER_AGENT = b"MyEditor"


def document_url(relay: str) -> Optional[str]:
    """Where ``relay``'s NIP-11 document is: its own address over https
    for wss:// and http for ws://; None for anything else."""
    key = signer_limits.relay_key(relay)
    if key is None:
        return None
    scheme, rest = key.split("://", 1)
    return ("https://" if scheme == "wss" else "http://") + rest


def cap_from_answer(raw: bytes) -> Optional[int]:
    """The content cap a NIP-11 document states (None for none). Raises
    ValueError when it is not a JSON object (RecursionError when nested
    past what json reads)."""
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("a NIP-11 document is a JSON object")
    return signer_limits.content_cap_of(document)


class _Waiter:
    def __init__(self, keys: Set[str], done: Callable[[], None]) -> None:
        self.keys = keys
        self.done = done


class RelayCapsReader(QObject):
    """Reads the NIP-11 documents of a connection's relays (see the
    module). ``nam`` is a seam for tests."""

    def __init__(self, *, nam: Optional[QNetworkAccessManager] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._nam = nam or QNetworkAccessManager(self)
        self._reading: Set[str] = set()
        self._waiters: List[_Waiter] = []

    def read(self, relays: Iterable[str], done: Optional[Callable[[], None]] = None) -> None:
        """Read the documents of ``relays`` not read yet in this run, and
        call ``done`` (on a later turn) once the connection's bound is
        known."""
        keys = {key for key in map(signer_limits.relay_key, relays or ()) if key}
        for key in sorted(keys):
            if not signer_limits.relay_read(key) and key not in self._reading:
                self._start(key)
        if done is not None:
            self._waiters.append(_Waiter(keys, done))
            QTimer.singleShot(0, self._settle)

    def _known(self, keys: Set[str]) -> bool:
        if any(signer_limits.relay_read(key) and signer_limits.connection_message_limit([key])
               == signer_limits.NIP44_MESSAGE_MAX for key in keys):
            return True                     # one relay without a cap decides it
        return not (keys & self._reading)

    def _settle(self) -> None:
        waiting, self._waiters = self._waiters, []
        for waiter in waiting:
            if self._known(waiter.keys):
                waiter.done()
            else:
                self._waiters.append(waiter)

    def _start(self, key: str) -> None:
        url = document_url(key)
        if url is None:
            return
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b"Accept", b"application/nostr+json")
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setTransferTimeout(NIP11_TIMEOUT_MS)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        for attribute in (QNetworkRequest.Attribute.CookieLoadControlAttribute,
                          QNetworkRequest.Attribute.CookieSaveControlAttribute):
            request.setAttribute(attribute, QNetworkRequest.LoadControl.Manual)
        request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                             QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        self._reading.add(key)
        reply = self._nam.get(request)
        if reply is None:
            QTimer.singleShot(0, lambda: self._finish(key, None, read=False))
            return
        # A relay that takes the connection and says nothing: setTransferTimeout
        # counts from the last byte, this from the start.
        timer = QTimer(reply)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(NIP11_TIMEOUT_MS)

        def progress(received: int, _total: int) -> None:
            if received > NIP11_MAX_BYTES:
                reply.abort()

        def finished() -> None:
            cap, read = None, False
            try:
                status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
                if reply.error() == QNetworkReply.NetworkError.NoError and status == 200:
                    raw = bytes(reply.readAll())
                    if len(raw) <= NIP11_MAX_BYTES:
                        cap, read = cap_from_answer(raw), True
            except (ValueError, UnicodeDecodeError, RecursionError):
                # Not JSON, not UTF-8, or nested past what json reads.
                cap, read = None, False
            finally:
                timer.stop()
                reply.deleteLater()
            self._finish(key, cap, read=read)

        reply.downloadProgress.connect(progress)
        reply.finished.connect(finished)

    def _finish(self, key: str, cap: Optional[int], *, read: bool) -> None:
        self._reading.discard(key)
        signer_limits.note_relay_cap(key, cap, read=read)
        self._settle()


__all__ = ["NIP11_MAX_BYTES", "NIP11_TIMEOUT_MS", "RelayCapsReader", "cap_from_answer",
           "document_url"]
