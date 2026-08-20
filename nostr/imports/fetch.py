# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared async source fetcher on top of ``QNetworkAccessManager``.

The one fetch path every resolver uses. Matches the rest of the editor
(avatar loader, blossom client, relay pool): callback-driven, no
threading, exactly one of ``on_success`` / ``on_failure`` fires per
call. Failures arrive as :class:`~nostr.imports.errors.SourceError`
with a stable code so callers never string-match transport errors.

Bounds:
- body size capped at 16 MiB, enforced mid-transfer (the reply is
  aborted the moment the cap is crossed, not after buffering),
- 30 s transfer timeout,
- redirect chain capped explicitly (Qt 6 follows redirects with its
  no-less-safe policy by default; the hop cap stops loops).

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

from .errors import ERROR_CODES, SourceError


_USER_AGENT = b"my-editor-rss/1"
_TRANSFER_TIMEOUT_MS = 30 * 1000          # 30s of idle time
_MAX_BODY_BYTES = 16 * 1024 * 1024        # 16 MiB hard cap on a single body
_MAX_REDIRECTS = 8                        # slash-fix / https-upgrade / www hops
_OVERSIZE_MESSAGE = (
    f"Feed exceeds the {_MAX_BODY_BYTES // (1024 * 1024)} MiB size limit"
)

_CHARSET_FROM_CONTENT_TYPE = re.compile(
    r"charset\s*=\s*([A-Za-z0-9_\-.:]+)", re.IGNORECASE
)
_CHARSET_FROM_XML_DECL = re.compile(
    rb"""<\?xml[^?>]*encoding\s*=\s*["']([A-Za-z0-9_\-.:]+)["']""", re.IGNORECASE
)


class SourceFetcher(QObject):
    """Reusable one-shot HTTP(S) body fetcher.

    Call :meth:`fetch` per request. The object owns one
    ``QNetworkAccessManager`` for the life of the instance.
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._nam = QNetworkAccessManager(self)

    def fetch(
        self,
        url: str,
        *,
        on_success: Callable[[str], None],
        on_failure: Callable[[SourceError], None],
    ) -> None:
        """Issue a GET and decode the response body as text.

        ``on_success`` receives the decoded body. ``on_failure`` receives
        a :class:`SourceError` with a stable code and a short reason.
        """
        qurl = QUrl(url)
        if not qurl.isValid() or qurl.scheme() not in ("http", "https"):
            on_failure(SourceError(
                "Feed URL must be http(s)", ERROR_CODES.FETCH_ERROR))
            return

        request = QNetworkRequest(qurl)
        request.setTransferTimeout(_TRANSFER_TIMEOUT_MS)
        request.setMaximumRedirectsAllowed(_MAX_REDIRECTS)
        request.setRawHeader(b"User-Agent", _USER_AGENT)
        request.setRawHeader(
            b"Accept",
            b"application/rss+xml, application/atom+xml, application/feed+json, "
            b"application/xml;q=0.9, */*;q=0.1",
        )

        reply = self._nam.get(request)
        # Abort mid-transfer the moment the byte cap is crossed instead
        # of buffering an arbitrarily large body first. ``abort()``
        # surfaces in ``finished`` as OperationCanceledError; the flag
        # lets the handler report a size rejection rather than a
        # generic network failure.
        oversize = {"hit": False}

        def _size_guard(received: int, _total: int, r=reply) -> None:
            if received > _MAX_BODY_BYTES and not oversize["hit"]:
                oversize["hit"] = True
                r.abort()

        reply.downloadProgress.connect(_size_guard)
        reply.finished.connect(
            lambda r=reply: self._on_finished(r, on_success, on_failure, oversize)
        )

    # -- internals ---------------------------------------------------------

    def _on_finished(
        self,
        reply: QNetworkReply,
        on_success: Callable[[str], None],
        on_failure: Callable[[SourceError], None],
        oversize: dict,
    ) -> None:
        try:
            if oversize["hit"]:
                on_failure(SourceError(_OVERSIZE_MESSAGE, ERROR_CODES.TOO_LARGE))
                return
            if reply.error() != QNetworkReply.NoError:
                on_failure(SourceError(
                    reply.errorString() or "network error",
                    ERROR_CODES.FETCH_ERROR,
                ))
                return

            data = bytes(reply.readAll())
            if not data:
                on_failure(SourceError(
                    "Empty response", ERROR_CODES.EMPTY_RESPONSE))
                return
            # Belt and braces: some backends deliver in one burst with
            # no intermediate progress signal before ``finished``.
            if len(data) > _MAX_BODY_BYTES:
                on_failure(SourceError(_OVERSIZE_MESSAGE, ERROR_CODES.TOO_LARGE))
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
