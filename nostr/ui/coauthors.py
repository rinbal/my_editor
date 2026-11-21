# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who writes an article with its author, added and removed the same way
in Article Details and in Publish as Article, and how the zaps to it are
shared between them.

A co-author is chosen with the window's person picker; the author is
never their own co-author, and nobody is added twice. A co-author is
credited and receives no zaps until given a share (nostr/zap_shares.py:
whole percentages, the author keeping what the others leave); one
removed gives their share back to the author. A co-author whose profile
has no lightning address can't receive zaps and gets no share: each is
asked about once (``can_receive_zaps``), counted in until answered, and
asked again the next time when no relay answered.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .. import zap_shares
from ..article_details import ArticleDetails
from ..outbox.defaults import INDEXER_RELAYS
from ..profiles import Profile

Coauthor = Tuple[str, str]          # pubkey, relay hint


class Coauthors:
    """The co-author rules both windows follow (see the module docstring).

    ``profile`` gives the author, ``relay_hint`` the relay a person's zap
    is announced on, and ``can_receive_zaps(pubkey, hint, answer)`` answers
    True, False, or None when no relay answered."""

    def __init__(self, *, profile: Callable[[], Optional[Profile]],
                 relay_hint: Callable[[str], str] = lambda pubkey: "",
                 can_receive_zaps: Optional[Callable[[str, str, Callable], None]] = None
                 ) -> None:
        self._profile = profile
        self._relay_hint = relay_hint
        self._can_receive_zaps = can_receive_zaps
        # pubkey -> True, False, or None while asking.
        self._zappable: Dict[str, Optional[bool]] = {}

    # -- changing them ----------------------------------------------------------

    def added(self, details: ArticleDetails, person) -> Optional[ArticleDetails]:
        """``details`` with ``person`` (a Person) a co-author, or None when
        they are one already or are the author."""
        own = self._profile()
        if any(pubkey == person.pubkey for pubkey, _hint in details.coauthors) or (
                own is not None and person.pubkey == own.user_pubkey):
            return None
        return self.changed(details,
                            details.coauthors + ((person.pubkey, person.relay_hint or ""),))

    def removed(self, details: ArticleDetails, pubkey: str) -> ArticleDetails:
        return self.changed(details, tuple(p for p in details.coauthors if p[0] != pubkey))

    def changed(self, details: ArticleDetails,
                coauthors: Sequence[Coauthor]) -> ArticleDetails:
        """``details`` with ``coauthors``. A co-author no longer among them
        gives their share back to the author; anyone else the zaps are
        shared with (as another app set them) keeps theirs, and the zap
        tags stay as written while nobody's share changes. The signer keeps
        their place among those kept (signer_place): after the co-authors
        kept before them, so one added comes after them; with no co-author
        left the place is not known any more, and the signer comes first."""
        kept = {pubkey for pubkey, _hint in coauthors}
        place = details.signer_place
        if place is not None and place >= 0:
            place = sum(1 for pubkey, _hint in details.coauthors[:place] if pubkey in kept)
        if not coauthors:
            place = None
        changed = details.with_changes(coauthors=tuple(coauthors), signer_place=place)
        gone = {pubkey for pubkey, _hint in details.coauthors if pubkey not in kept}
        shares = self.shares(details)
        if not gone & set(shares):
            return changed
        shares = {pubkey: share for pubkey, share in shares.items() if pubkey not in gone}
        return changed.with_changes(zaps=self._zaps_for(shares, changed))

    # -- sharing zaps -----------------------------------------------------------

    def shares(self, details: ArticleDetails) -> Dict[str, int]:
        """Everyone but the author the article's zaps go to, with their
        percentage."""
        own = self._profile()
        return zap_shares.shares_of(details.zaps, own.user_pubkey if own else "")

    def with_share(self, details: ArticleDetails, pubkey: str, share: int) -> ArticleDetails:
        """``details`` with ``pubkey``'s share of the zaps set to ``share``,
        held within what the others leave; none for someone who can't
        receive zaps."""
        if self._zappable.get(pubkey) is False:
            share = 0
        shares = zap_shares.with_share(self.shares(details), pubkey, share)
        return details.with_changes(zaps=self._zaps_for(shares, details))

    def split_evenly(self, details: ArticleDetails) -> ArticleDetails:
        """An equal share for the author and every co-author who can
        receive zaps (or is not known yet not to)."""
        shares = zap_shares.evenly([pubkey for pubkey, _hint in details.coauthors
                                    if self._zappable.get(pubkey) is not False])
        return details.with_changes(zaps=self._zaps_for(shares, details))

    def _zaps_for(self, shares: Dict[str, int], details: ArticleDetails):
        """The zap tags for ``shares``: each person named with the relay
        they were named with, their co-author hint, or where people's
        profiles are collected (an indexer), so every tag has one."""
        own = self._profile()
        if own is None:
            return details.zaps
        relays = {pubkey: hint for pubkey, hint in details.coauthors if hint}
        relays.update({pubkey: relay for pubkey, relay
                       in zap_shares.relays_of(details.zaps).items() if relay})
        relays.setdefault(own.user_pubkey, self._relay_hint(own.user_pubkey))
        return zap_shares.zap_tags(shares, own.user_pubkey, relays, INDEXER_RELAYS[0])

    # -- who can receive zaps ---------------------------------------------------

    def unzappable(self, coauthors: Sequence[Coauthor]) -> List[str]:
        """The co-authors known to have no lightning address."""
        return [pubkey for pubkey, _hint in coauthors if self._zappable.get(pubkey) is False]

    def ask(self, coauthors: Sequence[Coauthor],
            answered: Callable[[str, Optional[bool]], None]) -> None:
        """Ask once whether each co-author can receive zaps (their profile
        names a lightning address); ``answered(pubkey, able)`` follows."""
        if self._can_receive_zaps is None:
            return
        for pubkey, hint in coauthors:
            if pubkey in self._zappable:
                continue
            self._zappable[pubkey] = None
            self._can_receive_zaps(pubkey, hint, lambda able, key=pubkey: answered(key, able))

    def learned(self, details: ArticleDetails, pubkey: str,
                able: Optional[bool]) -> Optional[ArticleDetails]:
        """What ``details`` become now that ``pubkey`` is known to be able
        to receive zaps or not: a co-author who can't loses their share to
        the author; the same details when only what is shown changes, or
        None when nothing does. One whose profile couldn't be read (None)
        keeps their share and is asked about again the next time."""
        if able is None:
            self._zappable.pop(pubkey, None)
            return None
        self._zappable[pubkey] = able
        if not any(p == pubkey for p, _hint in details.coauthors):
            return None
        if not able and self.shares(details).get(pubkey):
            return self.with_share(details, pubkey, 0)
        return details
