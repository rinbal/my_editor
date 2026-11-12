# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the creators' own signed confirmations say (editorial contract
12.4 ``/confirmation``, Which revision is published; 11.3), read from
the verified ``confirmed`` actions alone, never from the View's
``confirmations`` or ``step`` (F5 M1). Moved out of :mod:`.publishing`,
which offers every name here as before.

- :func:`published_revision`: which revision a ``published`` report
  published.
- :func:`correction_taken`: the writer took the waiting correction.
- :func:`to_confirm`: whose confirmation of the accepted revision and
  its split is missing; a work the publication signs is ready for
  release only when nobody's is.
- :func:`signed_of`: both, as the rows and the publish run take them.

The web app's writer keeps the same two rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence, Tuple

from . import following as rules
from . import formats, views


def published_revision(items: Sequence[views.TimelineItem], sid: str,
                       report: views.TimelineItem, *, writer: str, accepted: Optional[int],
                       corrections: Mapping[str, int],
                       checks: rules.SignedChecks) -> Optional[int]:
    """The revision a ``published`` ``report`` published, from signed words
    only (12.4, Which revision is published; RG-M4-a 11): the verified
    accept's (``accepted``), or a later correction (``corrections``: the
    verified corrections opened here, by their hash) that the writer's
    verified confirmation named before the report, or, for a work the
    publication signs, the one its report names when that is one of them.
    Never the server's ``Item.event.revision``, so a later revision it
    claims cannot have the files of a correction nobody took deleted."""
    found = accepted
    taken = set(corrections.values())
    body = checks.body(report.event.signed, path=views.action_path("published", sid),
                       sid=sid) or {}
    named = body.get("revision")
    if report.event.signed.event.get("pubkey") != writer and formats.is_int(named) \
            and named in taken:
        found = max(found or 0, named)
    for said in verified_confirmations(items, sid, checks):
        if said.seq < report.seq and said.pubkey == writer and "correction" in said.confirms \
                and said.revision_hash in corrections:
            found = max(found or 0, corrections[said.revision_hash])
    return found


@dataclass(frozen=True)
class Confirmation:
    """A ``confirmed`` action as its verified signed body says it (12.4
    ``/confirmation``): who signed it, the revision and the split it
    names, what it confirms, and its ``seq``."""

    pubkey: str
    revision_hash: str
    split_rumor_id: Optional[str]
    confirms: Tuple[str, ...]
    seq: int


def verified_confirmations(items: Sequence[views.TimelineItem], sid: str,
                           checks: rules.SignedChecks) -> List[Confirmation]:
    """Every ``confirmed`` item whose stored signed action verifies (10:
    signature, payload, path, ``submission_id``) and is its actor's,
    oldest first, read from its signed body alone: what the phases that
    rest on a creator's word are taken from (F5 M1), never the View's
    ``confirmations``."""
    path = views.action_path("confirmed", sid)
    found = []
    for item in sorted(items, key=lambda each: each.seq):
        event = item.event
        if event is None or event.name != "confirmed" or event.signed is None:
            continue
        body = checks.body(event.signed, path=path, sid=sid)
        signer = event.signed.event.get("pubkey")
        confirms, named, split = (body or {}).get("confirms"), (body or {}).get(
            "revision_hash"), (body or {}).get("split_rumor_id")
        if body is None or signer != event.actor or not formats.is_hex64(named) \
                or not isinstance(confirms, list) \
                or not all(isinstance(what, str) for what in confirms) \
                or not (split is None or isinstance(split, str)):
            continue
        found.append(Confirmation(pubkey=signer, revision_hash=named, split_rumor_id=split,
                                  confirms=tuple(confirms), seq=item.seq))
    return found


def correction_taken(items: Sequence[views.TimelineItem], sid: str, *, writer: str,
                     revision_hash: str, checks: rules.SignedChecks) -> bool:
    """Whether the writer took the waiting correction (12.4, Which revision
    is published): their newest verified confirmation names
    ``confirms: ["correction"]`` with ``revision_hash``, the latest
    verified correction's. Never the View's word (F5 M1)."""
    mine = [said for said in verified_confirmations(items, sid, checks) if said.pubkey == writer]
    return bool(mine) and mine[-1].confirms == ("correction",) \
        and mine[-1].revision_hash == revision_hash


def to_confirm(items: Sequence[views.TimelineItem], sid: str, *, creators: Sequence[str],
               revision_hash: str, split_rumor_id: Optional[str],
               checks: rules.SignedChecks) -> Tuple[str, ...]:
    """The ``creators`` (the writer and the contributors the verified
    revision names) without a verified confirmation naming the accepted
    ``revision_hash`` and the accept's ``split_rumor_id`` (11.3, 12.4).
    A work the publication signs is ready for release only when none is
    left, whatever the View's ``step`` says (F5 M1)."""
    done = {said.pubkey for said in verified_confirmations(items, sid, checks)
            if said.revision_hash == revision_hash and said.split_rumor_id == split_rumor_id
            and "correction" not in said.confirms}
    return tuple(pk for pk in dict.fromkeys(creators) if pk not in done)


def signed_of(view: views.SubmissionView, items: Sequence[views.TimelineItem], *,
              verified: Mapping[int, object], accepted, checks: rules.SignedChecks
              ) -> rules.Signed:
    """:class:`~.following.Signed` for ``view``: ``verified`` the revisions
    opened here from their verified envelopes (``opening.VerifiedRevision``
    by number), ``accepted`` the verified accept (``publishing.Accepted``)
    or None."""
    sid = view.submission_id
    current = verified.get(view.revision)
    taken = current is not None and current.correction and correction_taken(
        items, sid, writer=view.writer, revision_hash=current.revision_hash, checks=checks)
    named = verified.get(accepted.revision) if accepted is not None and view.state == "accepted" \
        and view.signer == "publication" else None
    waiting = None
    if named is not None and not named.correction:
        waiting = to_confirm(items, sid, creators=(view.writer, *named.participants),
                             revision_hash=named.revision_hash,
                             split_rumor_id=accepted.split_rumor_id, checks=checks)
    return rules.Signed(taken=bool(taken), to_confirm=waiting)


__all__ = ["Confirmation", "correction_taken", "published_revision", "signed_of", "to_confirm",
           "verified_confirmations"]
