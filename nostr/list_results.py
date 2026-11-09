# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What reading and writing a private list (``nostr/encrypted_lists.py``)
can come to, as types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, List, Mapping, Optional, Tuple

from .paged_list import Page

__all__ = ["ListRead", "ListWords", "PageRead", "PublishResult", "PublishStatus", "ReadStatus"]


class ReadStatus(Enum):
    """What a read found."""

    COMPLETE = "complete"
    # Paged: a page is missing or of another generation. The value holds
    # every entry of every page that is the account's own.
    INCOMPLETE = "incomplete"
    MISSING = "missing"             # no list (no page 0)
    NO_ANSWER = "no answer"         # none of the account's own relays answered
    # A page the relays returned could not be opened now (the signer
    # refused, failed or did not answer): nothing is written until a
    # later read opens it (13). ``words`` say why; asking again may help.
    BLOCKED = "blocked"
    # Opened, but not this list (another format, not a page, more than
    # 64 pages, not taken in): never written over. ``words`` say so.
    UNREADABLE = "unreadable"


class PublishStatus(Enum):
    """How a publish ended."""

    UP_TO_DATE = "up to date"       # what is here is on the relays already
    SENT = "sent"                   # a complete generation, page 0 accepted
    NOT_ACCEPTED = "not accepted"   # no relay took every page
    FAILED = "failed"               # the signer refused, or the list cannot be written
    NO_ANSWER = "no answer"         # the read had no answer: nothing written
    BLOCKED = "blocked"             # a page could not be opened now: nothing written
    UNREADABLE = "unreadable"       # the list cannot be read: never written over


@dataclass(frozen=True)
class ListWords:
    """What the person is told when the list cannot be read, in the
    caller's words (they name the list)."""

    unreadable: str
    signer_silent: str
    old_encryption: str
    # Part of the list is missing (shown, when given, while the rest is
    # read and written as usual).
    missing_page: str = ""


@dataclass(frozen=True)
class PageRead:
    """A later page as read: its event and what it says."""

    event: dict
    page: Page


@dataclass(frozen=True)
class ListRead:
    """A read: ``value`` the decoded list (None when MISSING or not
    read), ``event`` page 0's, ``stale`` the later pages that are missing
    or of another generation, ``later`` every later page that counts,
    ``words`` what the person is told when it is BLOCKED or UNREADABLE
    (also said through the list's ``on_status``), and ``found`` the
    newest event the relays hold for each page, whether it counts or not
    (too long, not a page of this list, emptied): a write replaces them
    all, so it is dated after every one."""

    status: ReadStatus
    value: Any = None
    event: Optional[dict] = None
    stale: Tuple[int, ...] = ()
    later: Mapping[int, PageRead] = field(default_factory=dict)
    words: str = ""
    found: Tuple[dict, ...] = ()

    @property
    def readable(self) -> bool:
        return self.status not in (ReadStatus.NO_ANSWER, ReadStatus.BLOCKED,
                                   ReadStatus.UNREADABLE)

    def events(self) -> List[dict]:
        """Every event found, page 0's first: those that count and those
        that do not."""
        counted = ([self.event] if self.event else []) + [r.event for r in self.later.values()]
        ids = {event["id"] for event in counted}
        return counted + [event for event in self.found if event["id"] not in ids]


@dataclass(frozen=True)
class PublishResult:
    status: PublishStatus
    remote: Any = None          # UP_TO_DATE: the list read from the relays
    value: Any = None           # SENT: what was written
    signed: Optional[dict] = None   # SENT: the event of page 0
    reason: str = ""            # FAILED: why; BLOCKED, UNREADABLE: the read's words

    @property
    def on_relays(self) -> bool:
        """The write ended with what was here on the relays (written now,
        or there already). Whether a given entry survived the merge is
        the caller's question (a newer copy from another device wins)."""
        return self.status in (PublishStatus.SENT, PublishStatus.UP_TO_DATE)
