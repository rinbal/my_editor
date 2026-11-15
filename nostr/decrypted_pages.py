# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pages of one account's encrypted list that were opened already.

A list is read again quietly: until it is whole, after some relays did
not answer, after a write. Most of its pages are the ones opened last
time, and with a signer app every page opened is a request to it, which
some apps put in front of the person each time. A re-read must not ask
again for what it saw: a page opened is kept here, in memory only, by
its event id and the hash of its content, and a re-read asks the signer
only for pages it has not seen. A page this app wrote is kept as it was
written, so reading it back asks nothing either.

It belongs to one account. Its owner drops it when the account stops
(sign-out or another account, lesson 31): nothing of an account that was
let go stays readable here. It never holds more than a few complete
lists, so a list that keeps changing cannot grow it without end.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Optional, Tuple

from .paged_list import MAX_PAGES

__all__ = ["DecryptedPages"]

# A few generations of the longest list.
MAX_KEPT = 4 * MAX_PAGES


class DecryptedPages:
    """What the pages of ``account``'s list opened to."""

    def __init__(self, account: str, *, limit: int = MAX_KEPT) -> None:
        self.account = account.lower()
        self._limit = max(1, int(limit))
        self._texts: "OrderedDict[Tuple[str, str], str]" = OrderedDict()

    @staticmethod
    def _key(event: dict) -> Tuple[str, str]:
        content = str(event.get("content") or "")
        return (str(event.get("id") or ""),
                hashlib.sha256(content.encode("utf-8")).hexdigest())

    def belongs_to(self, account: str) -> bool:
        return bool(account) and account.lower() == self.account

    def get(self, event: dict) -> Optional[str]:
        """The plaintext ``event`` opened to, or None when it was not seen."""
        key = self._key(event)
        text = self._texts.get(key)
        if text is not None:
            self._texts.move_to_end(key)
        return text

    def put(self, event: dict, text: str) -> None:
        if not event.get("id"):
            return
        key = self._key(event)
        self._texts[key] = text
        self._texts.move_to_end(key)
        while len(self._texts) > self._limit:
            self._texts.popitem(last=False)

    def clear(self) -> None:
        self._texts.clear()

    def __len__(self) -> int:
        return len(self._texts)
