# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The body of ``POST /api/submissions`` (editorial contract 12.4).

:func:`submission_body` builds it with its ids derived here
(``coordinate_hash``, ``submission_id``, ``status_key_hash``), so a
caller cannot send one the server would compute differently;
:func:`submission_body_problem` says why a body must not be sent. The
body is the wire form (a dict of JSON values): its exact bytes are
signed and sent, and the receipt is compared with it field by field.
Pure: no Qt, no network.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from . import formats
from .formats import FormatError

def submission_body(*, writer: str, publication: str, key_event_id: str, epoch: int,
                    d: str, round_: int, revision: int, revision_hash: str, kind: str,
                    title: str, signer: str, participants: Sequence[str], p_sub: str,
                    envelope: str, envelope_sha256: str, wraps: Mapping[str, Mapping[str, str]],
                    blobs: Sequence[Mapping[str, Any]], status_key: str,
                    client: str) -> Dict[str, Any]:
    """The body of ``POST /api/submissions``, its ids derived here
    (``coordinate_hash``, ``submission_id``, ``status_key_hash``), so a
    caller cannot send one that the server would compute differently."""
    coordinate = formats.coordinate_hash(writer, d)
    body = {
        "v": formats.CONTRACT_VERSION,
        "submission_id": formats.submission_id(writer, publication, coordinate, round_),
        "round": round_,
        "revision": revision,
        "revision_hash": revision_hash,
        "publication": publication,
        "key_event_id": key_event_id,
        "epoch": epoch,
        "kind": kind,
        "title": title,
        "coordinate_hash": coordinate,
        "signer": signer,
        "participants": list(participants),
        "p_sub": p_sub,
        "envelope": envelope,
        "envelope_sha256": envelope_sha256,
        "wraps": {name: dict(wrap) for name, wrap in wraps.items()},
        "blobs": [dict(blob) for blob in blobs],
        "status_key_hash": formats.status_key_hash(status_key),
        "client": client,
    }
    problem = submission_body_problem(body)
    if problem:
        raise FormatError(problem)
    return body


def submission_body_problem(body: Any) -> Optional[str]:
    """Why ``body`` must not be sent, or None. The server checks all of
    this too; checked here, a mistake costs no signature and no upload."""
    if not isinstance(body, dict):
        return "the body must be an object"
    try:
        formats.need(body.get("v") == formats.CONTRACT_VERSION, "the body's version")
        formats.need(formats.is_hex64(body.get("submission_id")), "the submission id")
        formats.need(formats.is_int(body.get("round"), low=1), "the round")
        formats.need(formats.is_int(body.get("revision"), low=1), "the revision")
        formats.need(formats.is_hex64(body.get("revision_hash")), "the revision hash")
        formats.parse_address(body.get("publication"))
        formats.need(formats.is_hex64(body.get("key_event_id")), "the key event")
        formats.need(formats.is_int(body.get("epoch")), "the epoch")
        formats.need(body.get("kind") in formats.KINDS, "the kind")
        title = body.get("title")
        formats.need(isinstance(title, str) and 0 < len(title) <= formats.MAX_TITLE_CHARS,
                     f"the title has 1 to {formats.MAX_TITLE_CHARS} characters")
        formats.refuse_secret_keys([title])
        formats.need(body.get("signer") in formats.SIGNERS, "who signs")
        participants = body.get("participants")
        formats.need(isinstance(participants, list)
                     and len(participants) <= formats.MAX_PARTICIPANTS
                     and all(formats.is_pubkey(p) for p in participants)
                     and len(set(participants)) == len(participants),
                     f"at most {formats.MAX_PARTICIPANTS} people take part")
        formats.need(formats.is_pubkey(body.get("p_sub")), "the submission key")
        wraps = body.get("wraps")
        formats.need(isinstance(wraps, dict) and {"redaktion", "writer"} <= set(wraps)
                     and all(formats.is_wrap(w) for w in wraps.values()),
                     "the wraps for the editorial team and the writer")
        formats.need(set(wraps) - {"redaktion", "writer"} <= set(participants),
                     "a wrap for someone who does not take part")
        blobs = body.get("blobs")
        formats.need(isinstance(blobs, list) and all(
            isinstance(b, dict) and formats.is_hex64(b.get("sha256")) for b in blobs),
            "the files")
        formats.need(len({b["sha256"] for b in blobs}) == len(blobs), "a file listed twice")
        formats.need(formats.is_hex64(body.get("status_key_hash")), "the status key hash")
        if "source" in body:
            formats.need(isinstance(body["source"], dict), "the source")
        else:
            envelope = formats.b64decode(body.get("envelope"),
                                         limit=formats.MAX_ENVELOPE_BYTES)
            formats.need(formats.sha256_hex(envelope) == body.get("envelope_sha256"),
                         "the envelope hash")
    except FormatError as exc:
        return str(exc)
    return None


__all__ = ["submission_body", "submission_body_problem"]
