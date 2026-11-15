# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The address of the person's Bunker46, for Connect Signer App
(nostr/ui/connect_dialog.py): what counts as one as typed, the page of it
that takes a connection link, and the one kept in the settings.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import quote, urlsplit

from PySide6.QtCore import QByteArray, QUrl

import settings
from url_safety import is_safe_external_url

# Where the address of the person's Bunker46 is kept (settings.json).
BUNKER46_SETTING = "bunker46_address"


def bunker46_address(text: str) -> Optional[str]:
    """The address of a Bunker46 as typed: https only, a host, no user
    name, query or fragment, without a trailing slash; a bare host gets
    https. None when it is not one. A Bunker46 runs where the person runs
    it, also on their own network, so a local name is fine."""
    text = (text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        parts.port                                  # raises for a port that is no number
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not (parts.hostname or "").strip():
        return None
    if "@" in parts.netloc or parts.query or parts.fragment or any(c.isspace() for c in text):
        return None
    # The host as DNS has it (IDNA: "bünker.example" is
    # "xn--bnker-kva.example"), and the path in ASCII, every escape a
    # real one, so the link built from it is a URL (bunker46_link).
    try:
        host = parts.hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if ":" in host:
        host = f"[{host}]"                            # an IPv6 address
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    if re.search(r"%(?![0-9A-Fa-f]{2})", parts.path):
        return None
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    address = f"https://{host}{path}".rstrip("/")
    return address if is_safe_external_url(address) else None


def bunker46_link(address: str, uri: str) -> QUrl:
    """The page of ``address`` (a Bunker46) that takes the connection
    link ``uri``: ``/connections?nostrconnect=<uri, encoded>`` (Bunker46's
    router, research/signer-pairing.md section 6)."""
    encoded = f"{address}/connections?nostrconnect={quote(uri, safe='')}"
    return QUrl.fromEncoded(QByteArray(encoded.encode("ascii")), QUrl.ParsingMode.StrictMode)


def remembered_bunker46() -> Optional[str]:
    """The Bunker46 address kept in the settings, if it is still one."""
    return bunker46_address(str(settings.load_settings().get(BUNKER46_SETTING) or ""))


__all__ = ["BUNKER46_SETTING", "bunker46_address", "bunker46_link", "remembered_bunker46"]
