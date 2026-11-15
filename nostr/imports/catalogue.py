# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which posts are already imported: the rule both apps follow.

A post counts as imported (and is never imported again, so a draft the
person edited is never overwritten, and a deleted one never comes back)
when, for the account and the post's identifier, any of these exists:

1. a draft wrap (kind 31234, also one whose content was blanked to
   delete it), or a plain article draft (kind 30024);
2. a published article (kind 30023);
3. a deletion request (kind 5) naming the draft or the article;
4. an entry in this computer's ledger (inbox_store.py), which keeps a
   post imported after its draft expired on the relays.

EINUNDZWANZIG STANDUP answers the same question from its server. This
app has no server, so :class:`ExistingCatalogue` asks the relays: the
relays the account's drafts are written to (and read from) for drafts
and deletions, its outbox for articles, plus the DraftStore (drafts
already loaded, and deletions it saw) and the ledger.

It asks in requests every relay accepts: ``CHUNK`` identifiers a
request, at most two filters (NIP-11 lets a relay cap the filters of a
request; rnostr allows ten, and a request past that is refused), one
request after the other.

It fails closed: an answer counts for the drafts only when one of the
relays drafts are written to answered it (a fallback relay that has
nothing proves nothing), and when any request goes unanswered the
answer is "unavailable", never "nothing found", and an import does not
start. An event counts only when the account signed it.

What was found is judged once every request is answered: a draft or an
article a deletion request covers (nostr/deletion.py: a version no newer
than the request) counts as removed, though a relay that ignores the
request still hands it out; a version newer than the request counts as
what it is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set

from PySide6.QtCore import QObject

from i18n import _

from ..deletion import DELETION_KIND, DeletionRequest, read_deletion
from ..drafts import DRAFT_WRAP_KIND
from ..events import verify_event
from ..outbox import ask_draft_relays
from ..outbox.policy import normalize_relay_url
from ..queries import fetch_events
from .inbox_store import DRAFTED, PUBLISHED, REMOVED

# Identifiers a request: two filters of them stay far below what relays
# accept for a message (strfry: 128 KiB).
CHUNK = 100
ARTICLE_KIND = 30023
ARTICLE_DRAFT_KIND = 30024
QUERY_TIMEOUT_MS = 8_000

UNAVAILABLE = _("Your existing drafts couldn't be checked first: check your internet "
                "connection, then choose Resume.")

_RANK = {REMOVED: 1, DRAFTED: 2, PUBLISHED: 3}


@dataclass
class Existing:
    """What exists for the identifiers looked up: ``{d: state}``."""

    states: Dict[str, str] = field(default_factory=dict)

    def add(self, d_tag: str, state: str) -> None:
        if _RANK[state] > _RANK.get(self.states.get(d_tag, ""), 0):
            self.states[d_tag] = state

    def __contains__(self, d_tag: str) -> bool:
        return d_tag in self.states


# query(relays, filters, on_done(events, answered)): ``answered`` is the
# set of relays (normalized addresses) that ended their stored events
# (EOSE) before the timeout; one that refused the request is not in it.
Query = Callable[[List[str], List[dict], Callable[[List[dict], Set[str]], None]], None]


class ExistingCatalogue(QObject):
    """Looks up which identifiers already exist for an account."""

    def __init__(self, *, relay_pool, relay_directory, draft_store=None,
                 ledger: Optional[Callable[[Iterable[str]], Dict[str, str]]] = None,
                 entitled_relays=None, query: Optional[Query] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._draft_store = draft_store
        self._ledger = ledger
        self._entitled_relays = entitled_relays
        self._query = query or self._relay_query

    def known_locally(self, d_tag: str) -> Optional[str]:
        """What this computer already knows about ``d_tag``, without asking
        the relays: the DraftStore and the ledger. For the last check just
        before a draft is signed (the other app may have made one since)."""
        found = Existing()
        self._add_local(found, [d_tag])
        return found.states.get(d_tag)

    def look_up(self, profile, d_tags: Sequence[str], *,
                on_ready: Callable[[Existing], None],
                on_unavailable: Callable[[str], None]) -> None:
        tags = list(dict.fromkeys(d for d in d_tags if d))
        found = Existing()
        self._add_local(found, tags)
        if not tags:
            on_ready(found)
            return
        pubkey = profile.user_pubkey.lower()
        pending = {"sets": 2, "failed": False}
        evidence = _Evidence(pubkey, set(tags))

        def _set_done(ok: bool) -> None:
            if pending["failed"]:
                return
            if not ok:
                pending["failed"] = True
                on_unavailable(UNAVAILABLE)
                return
            pending["sets"] -= 1
            if pending["sets"] == 0:
                # Judged together: a deletion asked of the drafts' relays
                # covers an article found in the outbox too.
                evidence.judge(found)
                on_ready(found)

        chunks = [tags[i:i + CHUNK] for i in range(0, len(tags), CHUNK)]

        def _private_filters(chunk: List[str]) -> List[dict]:
            addresses = [f"{kind}:{pubkey}:{d}" for d in chunk
                         for kind in (DRAFT_WRAP_KIND, ARTICLE_KIND)]
            return [{"kinds": [DRAFT_WRAP_KIND, ARTICLE_DRAFT_KIND], "authors": [pubkey],
                     "#d": chunk},
                    {"kinds": [DELETION_KIND], "authors": [pubkey], "#a": addresses}]

        def _private(own: List[str]) -> None:
            # Asked of the read set (the drafts' own relays and the
            # fallback ones); an answer counts only from the own ones.
            ask_draft_relays(self._relay_directory, profile,
                             lambda read: self._ask_chunks(
                                 read, [_private_filters(c) for c in chunks], evidence,
                                 _set_done, counted=own),
                             entitled=self._entitled_relays, reading=True)

        def _outbox(relays: List[str]) -> None:
            self._ask_chunks(relays, [[{"kinds": [ARTICLE_KIND], "authors": [pubkey],
                                        "#d": chunk}] for chunk in chunks],
                             evidence, _set_done)

        ask_draft_relays(self._relay_directory, profile, _private,
                         entitled=self._entitled_relays, reading=False)
        self._relay_directory.outbox_of(pubkey, _outbox)

    # -- internals -------------------------------------------------------------

    def _add_local(self, found: Existing, tags: Iterable[str]) -> None:
        tags = list(tags)
        store = self._draft_store
        if store is not None:
            deleted = set(store.deleted_identifiers())
            for d_tag in tags:
                if d_tag in store:
                    found.add(d_tag, DRAFTED)
                elif d_tag in deleted:
                    found.add(d_tag, REMOVED)
        if self._ledger is not None:
            for d_tag, state in self._ledger(tags).items():
                found.add(d_tag, state)

    def _ask_chunks(self, relays, requests: List[List[dict]], evidence: "_Evidence",
                    done: Callable[[bool], None], *,
                    counted: Optional[Iterable[str]] = None) -> None:
        """Ask ``requests`` one after the other; ``done(True)`` when every
        one was answered (by one of ``counted``, when given), else
        ``done(False)`` at the first that was not."""
        relays = list(relays)
        counts = {normalize_relay_url(u) or u for u in (counted or ())}
        if not relays or not requests:
            done(bool(relays))
            return
        queue = list(requests)

        def _next() -> None:
            filters = queue.pop(0)
            self._query(relays, filters, _answer)

        def _answer(events: List[dict], answered: Set[str]) -> None:
            answered = {normalize_relay_url(u) or u for u in answered}
            if not answered or (counts and not (answered & counts)):
                done(False)
                return
            for event in events:
                evidence.add(event)
            if queue:
                _next()
            else:
                done(True)

        _next()

    def _relay_query(self, relays, filters, on_done) -> None:
        # Which relays answered, not how many: only an answer from the
        # relays drafts are written to counts (see the module docstring).
        # An answer cut off by a flooding relay is no answer: what it left
        # out could be the draft or the deletion that decides.
        fetch_events(self._relay_pool, relays, filters,
                     lambda fetched: on_done(list(fetched.events),
                                             set(fetched.answered) if fetched.complete
                                             else set()),
                     timeout_ms=QUERY_TIMEOUT_MS, parent=self)


class _Evidence:
    """The account's events found for the identifiers looked up, judged
    once every request is answered (review L2: a deletion newer than the
    draft gave "drafted" while any relay kept the draft)."""

    def __init__(self, pubkey: str, wanted: Set[str]) -> None:
        self._pubkey = pubkey
        self._wanted = wanted
        self._requests: List[DeletionRequest] = []
        self._versions: List[tuple] = []        # (d, state, event)

    def add(self, event: dict) -> None:
        if not isinstance(event, dict) or str(event.get("pubkey", "")).lower() != self._pubkey:
            return
        kind = event.get("kind")
        if kind == DELETION_KIND:
            request = read_deletion(event, self._pubkey)
            if request is not None:
                self._requests.append(request)
            return
        if not verify_event(event):
            return
        d_tag = next((t[1] for t in event.get("tags", [])
                      if isinstance(t, list) and len(t) >= 2 and t[0] == "d"), "")
        if d_tag not in self._wanted:
            return
        if kind == ARTICLE_KIND:
            self._versions.append((d_tag, PUBLISHED, event))
        elif kind == DRAFT_WRAP_KIND:
            self._versions.append((d_tag, DRAFTED if str(event.get("content") or "")
                                   else REMOVED, event))
        elif kind == ARTICLE_DRAFT_KIND:
            self._versions.append((d_tag, DRAFTED, event))

    def judge(self, found: Existing) -> None:
        # A deleted draft or article is named by its address: removed,
        # unless a version newer than the request says otherwise.
        for request in self._requests:
            for d_tag in (request.identifiers(DRAFT_WRAP_KIND)
                          + request.identifiers(ARTICLE_KIND)):
                if d_tag in self._wanted:
                    found.add(d_tag, REMOVED)
        for d_tag, state, event in self._versions:
            covered = any(request.covers(event) for request in self._requests)
            found.add(d_tag, REMOVED if covered else state)
