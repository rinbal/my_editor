# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Before the writer's work goes out (editorial contract 12.4; RG-M4-a
2): whom a work at the writer's address credits, and whether the
writer's work may go out over it. Moved out of :mod:`.publishing`,
which offers every name here as before."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from .. import events as nostr_events
from ..article_details import AUTHOR_MARKER
from . import formats


def credited_writer(event: Mapping) -> Optional[str]:
    """Who wrote a work, as its first ``["p", pk, relay, "author"]`` tag
    names them (section 5's credit; contract 10 at 53ff043: a work the
    publication signs is its writer's only so), or None for none."""
    for tag in event.get("tags") or ():
        if isinstance(tag, list) and len(tag) >= 4 and tag[0] == "p" \
                and tag[3] == AUTHOR_MARKER:
            return tag[1] if formats.is_hex64(tag[1]) else None
    return None


def held_credit(event: Any, address: str) -> str:
    """Whom an event held at ``address`` credits as its author (its first
    author tag, else its signer), or ``""`` for an event that is no valid
    kind 30023 at that address."""
    if not nostr_events.is_verified_event(event, kind=formats.ARTICLE_KIND) \
            or f"{formats.ARTICLE_KIND}:{event['pubkey']}:{formats.tag_value(event, 'd')}" \
            != address:
        return ""
    return credited_writer(event) or event["pubkey"]


def writer_may_release(credited: Optional[str], *, address_taken: bool, writer: str) -> bool:
    """Before the writer's work goes out (12.4, vector ``writer_release``):
    nothing goes out when the server says another submission's work stands
    at the writer's address, or when what it holds there (``credited``:
    whom that article credits; None: nothing there) is another person's.
    The writer's own earlier version may be replaced."""
    return not address_taken and (credited is None or credited == writer)


__all__ = ["credited_writer", "held_credit", "writer_may_release"]
