# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stable error codes + friendly copy for the importer.

Every failure that can reach the user travels as a :class:`SourceError`
carrying a machine-readable ``code`` from :data:`ERROR_CODES` plus a raw
message. The UI renders :func:`friendly_message`; the raw message stays
available for diagnostics. Resolvers added later contribute their codes
here so the whole importer shares one taxonomy.
"""

from __future__ import annotations

import logging
from typing import Optional

from i18n import _

_log = logging.getLogger(__name__)


class ERROR_CODES:
    """Namespace of stable error-code strings. Not an enum on purpose:
    codes cross signal boundaries and settle files as plain strings."""

    # Transport layer.
    FETCH_ERROR = "FETCH_ERROR"
    TOO_LARGE = "TOO_LARGE"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    # The address, or a redirect, points into the local network.
    LOCAL_NETWORK = "LOCAL_NETWORK"

    # Resolution / format layer.
    NOT_A_FEED = "NOT_A_FEED"
    NO_FEED_FOUND = "NO_FEED_FOUND"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    # A resolver-specific failure whose message is already user-ready
    # (e.g. "GitHub: API rate limit exceeded"); shown verbatim.
    SOURCE_ERROR = "SOURCE_ERROR"

    # Nostr source resolvers.
    NOSTR_INVALID = "NOSTR_INVALID"
    NOSTR_NOT_FOUND = "NOSTR_NOT_FOUND"
    NIP05_NOT_FOUND = "NIP05_NOT_FOUND"
    NO_RELAY_ACCESS = "NO_RELAY_ACCESS"

    # Platform exports.
    WXR_EMPTY = "WXR_EMPTY"
    GHOST_EMPTY = "GHOST_EMPTY"
    ARCHIVE_UNREADABLE = "ARCHIVE_UNREADABLE"

    # Web source resolvers.
    SITEMAP_EMPTY = "SITEMAP_EMPTY"
    BLUESKY_INVALID = "BLUESKY_INVALID"
    BLUESKY_NOT_FOUND = "BLUESKY_NOT_FOUND"
    BLUESKY_NOT_THREAD = "BLUESKY_NOT_THREAD"

    UNKNOWN = "UNKNOWN"


class SourceError(Exception):
    """A failure with a stable code and a raw (diagnostic) message."""

    def __init__(self, message: str, code: str = ERROR_CODES.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code

    @property
    def message(self) -> str:
        return str(self.args[0]) if self.args else ""


# Friendly copy per code. Wording is part of the UX: calm, actionable,
# and (per the platform writing guidelines) never "we", never blame,
# always a next step where one exists: one sentence each, its cause and
# what to do (lessons 36). A resolver's own words (SOURCE_ERROR) are
# shown as they are; any other code says GENERIC, its message only in
# the log (failure_words).
# The owner's decision Q-6: such an address is not followed, and the
# message says why in plain words.
LOCAL_NETWORK_MESSAGE = _(
    "This address is on this computer or its local network: MyEditor follows public websites "
    "only.")
# Whatever else kept a source or file from being read.
GENERIC = _("This couldn't be imported: check the address or the file and try again.")

_FRIENDLY = {
    ERROR_CODES.LOCAL_NETWORK: LOCAL_NETWORK_MESSAGE,
    ERROR_CODES.FETCH_ERROR: _(
        "This address couldn't be reached: check it and your internet connection, then try "
        "again."
    ),
    ERROR_CODES.TOO_LARGE: _(
        "This is too large to import: import its posts from an export file instead."
    ),
    ERROR_CODES.NO_FEED_FOUND: _(
        "No feed found at this site: paste the feed's address, or look for an RSS or Atom "
        "link in the page footer."
    ),
    ERROR_CODES.NOT_A_FEED: _(
        "That address doesn't give a feed: paste the address of its RSS, Atom or JSON feed."
    ),
    ERROR_CODES.UNSUPPORTED_SOURCE: _(
        "This kind of source can't be imported yet: try the address of its feed."
    ),
    ERROR_CODES.EMPTY_RESPONSE: _(
        "The site sent nothing back: try again in a moment."
    ),
    ERROR_CODES.NOSTR_INVALID: _(
        "That isn't a recognisable Nostr address: copy it again from where you found it."
    ),
    ERROR_CODES.NOSTR_NOT_FOUND: _(
        "That post wasn't found: it may have been deleted, or try again in a moment."
    ),
    ERROR_CODES.NIP05_NOT_FOUND: _(
        "No Nostr profile is published at that address: check how it is spelled."
    ),
    ERROR_CODES.NO_RELAY_ACCESS: _(
        "Importing from Nostr needs a connection: check your internet connection and try "
        "again."
    ),
    ERROR_CODES.WXR_EMPTY: _(
        "No published posts or pages were found in that WordPress export: export them again "
        "with “All content” chosen."
    ),
    ERROR_CODES.GHOST_EMPTY: _(
        "No published posts were found in that Ghost export: export it again from Ghost’s "
        "settings."
    ),
    ERROR_CODES.ARCHIVE_UNREADABLE: _(
        "Couldn't read that archive: export it again from the platform and try again."
    ),
    ERROR_CODES.SITEMAP_EMPTY: _(
        "That sitemap had no articles to import: try the site's feed instead."
    ),
    ERROR_CODES.BLUESKY_INVALID: _(
        "That is not a Bluesky post link: copy the link of the thread's first post."
    ),
    ERROR_CODES.BLUESKY_NOT_FOUND: _(
        "That Bluesky post could not be found: it may have been deleted."
    ),
    ERROR_CODES.BLUESKY_NOT_THREAD: _(
        "That is a single Bluesky post, not a thread: only threads of several posts become an "
        "article."
    ),
}


def friendly_message(error: SourceError, *, detail: Optional[str] = None) -> str:
    """Headline copy for ``error``, suitable to show a user directly: its
    code's one sentence, a resolver's own words as they are, or GENERIC.
    What went wrong (a transport's errorString, an exception) is never in
    it: :func:`failure_words` logs it. ``detail`` is kept for callers."""
    mapped = _FRIENDLY.get(error.code)
    if mapped:
        return mapped
    if error.code == ERROR_CODES.SOURCE_ERROR and error.message:
        return error.message
    return GENERIC


def failure_words(error: BaseException) -> str:
    """The one sentence for a failure of any kind (a SourceError, or what
    a reader raised), its own message only in the log (lessons 36): the
    one place the importer turns a failure into words."""
    if isinstance(error, SourceError):
        if error.message:
            _log.info("import failed (%s): %s", error.code, error.message[:200])
        return friendly_message(error)
    _log.info("import failed: %s", str(error)[:200])
    return GENERIC
