# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async feed fetch on top of ``QNetworkAccessManager``.

Matches the rest of the editor (avatar loader, blossom client, relay
pool): callback-driven, no threading, exactly one of ``on_success`` /
``on_failure`` fires per call.

Decoding is intentionally permissive: feeds in the wild lie about
encoding, so we fall back through ``Content-Type charset`` to the XML
declaration to UTF-8 with replacement on errors.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from PySide6.QtCore import QObject, QUrl
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)


_USER_AGENT = b"my-editor-rss/1"
_TRANSFER_TIMEOUT_MS = 30 * 1000          # 30s of idle time
_MAX_BODY_BYTES = 16 * 1024 * 1024        # 16 MiB hard cap on a single feed

_CHARSET_FROM_CONTENT_TYPE = re.compile(
    r"charset\s*=\s*([A-Za-z0-9_\-.:]+)", re.IGNORECASE
)
_CHARSET_FROM_XML_DECL = re.compile(
    rb"""<\?xml[^?>]*encoding\s*=\s*["']([A-Za-z0-9_\-.:]+)["']""", re.IGNORECASE
)


class FeedFetcher(QObject):
    """One-shot HTTPS feed fetcher.

    Reusable: call :meth:`fetch` again to issue another request. The
    object owns one ``QNetworkAccessManager`` for the life of the
    instance.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)

    def fetch(
        self,
        url: str,
        *,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        """Issue a GET and decode the response body as text.

        ``on_success`` receives the decoded body. ``on_failure`` receives
        a short, user-readable reason.
        """
        qurl = QUrl(url)
        if not qurl.isValid() or qurl.scheme() not in ("http", "https"):
            on_failure("Feed URL must be http(s)")
            return

        request = QNetworkRequest(qurl)
        request.setTransferTimeout(_TRANSFER_TIMEOUT_MS)
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setRawHeader(
            b"Accept",
            b"application/rss+xml, application/atom+xml, application/feed+json, "
            b"application/xml;q=0.9, */*;q=0.1",
        )

        reply = self._nam.get(request)
        reply.finished.connect(
            lambda r=reply: self._on_finished(r, on_success, on_failure)
        )

    # -- internals ---------------------------------------------------------

    def _on_finished(
        self,
        reply: QNetworkReply,
        on_success: Callable[[str], None],
        on_failure: Callable[[str], None],
    ) -> None:
        try:
            if reply.error() != QNetworkReply.NoError:
                on_failure(reply.errorString() or "network error")
                return

            data = bytes(reply.readAll())
            if not data:
                on_failure("Empty response")
                return
            if len(data) > _MAX_BODY_BYTES:
                on_failure(f"Feed exceeds {_MAX_BODY_BYTES} bytes")
                return

            content_type_var = reply.header(QNetworkRequest.ContentTypeHeader)
            content_type = str(content_type_var) if content_type_var else ""
            text = _decode_body(data, content_type)
            on_success(text)
        finally:
            reply.deleteLater()


def _decode_body(data: bytes, content_type: str) -> str:
    """Best-effort byte-to-text decode.

    Priority: ``Content-Type charset`` then the XML declaration's
    ``encoding`` attribute then UTF-8 with replacement.
    """
    charset: Optional[str] = None
    match = _CHARSET_FROM_CONTENT_TYPE.search(content_type or "")
    if match:
        charset = match.group(1)
    if not charset:
        decl = _CHARSET_FROM_XML_DECL.search(data[:512])
        if decl:
            charset = decl.group(1).decode("ascii", errors="ignore")
    if not charset:
        charset = "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return data.decode("utf-8", errors="replace")
