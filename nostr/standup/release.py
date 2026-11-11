# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Before the writer's work goes out (editorial contract 12.4; RG-M4-a 2,
RG-B5-d), as ``releaseRelays`` in the contract's
docs/editorial-fixtures/crosscheck.reference.mjs has it (contract
569b3f8, vector ``writer_release``). Moved out of :mod:`.publishing`,
which offers every name here as before.

Before any picture goes up in plain and again right before the writer
signs, the app reads the work's address in every place it goes:
``address_taken`` of the submission, what this server holds there
(``GET /api/articles/<address>``), and every relay the work goes to
(until EOSE). An event counts where it verifies and stands at that
address; whom it credits is its first ``["p", pk, relay, "author"]``
tag, else its signer. Nothing goes anywhere when that is someone other
than the writer, also on a relay that did not finish its read; nor when
this server's read failed or no relay answered. Otherwise the work goes
only to the relays that answered completely; the writer's own earlier
version there may be replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

from .. import events as nostr_events
from ..article_details import AUTHOR_MARKER
from . import formats


def credited_writer(event: Mapping) -> Optional[str]:
    """Who wrote a work, as its first ``["p", pk, relay, "author"]`` tag
    names them (section 5's credit; contract 10: a work the publication
    signs is its writer's only so), or None for none."""
    for tag in event.get("tags") or ():
        if isinstance(tag, list) and len(tag) >= 4 and tag[0] == "p" \
                and tag[3] == AUTHOR_MARKER:
            return tag[1] if formats.is_hex64(tag[1]) else None
    return None


def held_credit(event: Any, address: str) -> Optional[str]:
    """Whom an event at ``address`` credits as its author: the first
    author tag's key as it stands, else the signer. None for an event
    that is no valid kind 30023 at that address (it is in nobody's way)."""
    if not nostr_events.is_verified_event(event, kind=formats.ARTICLE_KIND) \
            or f"{formats.ARTICLE_KIND}:{event['pubkey']}:{formats.tag_value(event, 'd')}" \
            != address:
        return None
    first = next((tag for tag in event["tags"] if len(tag) >= 4 and tag[0] == "p"
                  and tag[3] == AUTHOR_MARKER), None)
    return first[1] if first is not None else event["pubkey"]


@dataclass(frozen=True)
class Release:
    """Where the writer's work may go: ``send_to``, the relays (none:
    nothing goes anywhere); ``someone_elses``, another person's work
    stands at the address (else nothing could be checked)."""

    send_to: Tuple[str, ...] = ()
    someone_elses: bool = False


def release(*, writer: str, address: str, address_taken: bool, server_read: bool,
            held: Optional[str], relays: Mapping[str, Tuple[bool, Sequence[Any]]]) -> Release:
    """``held``: whom the article this server holds at ``address``
    credits (the read's ``author_pubkey``; None: nothing there);
    ``relays``: each relay's read, ``(complete, events)``."""
    if address_taken or (held is not None and held != writer) or any(
            held_credit(event, address) not in (None, writer)
            for _complete, events in relays.values() for event in events):
        return Release(someone_elses=True)
    if not server_read:
        return Release()
    return Release(send_to=tuple(url for url, (complete, _events) in relays.items()
                                 if complete))


__all__ = ["Release", "credited_writer", "held_credit", "release"]
