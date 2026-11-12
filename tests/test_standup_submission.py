# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The body of a submission (nostr/standup/submission.py, contract 12.4):
built with the ids the server recomputes, and refused here, before any
signature or upload, for every mistake the server would refuse."""

from __future__ import annotations

import pytest

from nostr.standup import submission
from nostr.standup.formats import FormatError
from tests.standup_fakes import submission_for
from tests.standup_vectors import SID, VECTORS, pk, sk


def test_the_body_is_the_one_the_contract_describes():
    # submission_for assembles it by hand from 12.4; submission_body
    # derives the ids itself.
    prepared = submission_for(sk("writer"), participants=(pk("contributor"),))
    hand = prepared.body
    built = submission.submission_body(
        writer=pk("writer"), publication=hand["publication"], key_event_id=hand["key_event_id"],
        epoch=hand["epoch"], d="ein-jahr-mit-lightning", round_=1, revision=1,
        revision_hash=hand["revision_hash"], kind="article", title=hand["title"],
        signer="writer", participants=hand["participants"], p_sub=hand["p_sub"],
        envelope=hand["envelope"], envelope_sha256=hand["envelope_sha256"], wraps=hand["wraps"],
        blobs=[], status_key=prepared.status_key, client="MyEditor tests")
    assert built == hand
    assert built["submission_id"] == SID and built["coordinate_hash"] == \
        VECTORS["ids"]["coordinate_hash"]


@pytest.mark.parametrize("change", [
    lambda b: dict(b, envelope_sha256="00" * 32),
    lambda b: dict(b, wraps={"writer": b["wraps"]["writer"]}),
    lambda b: dict(b, wraps=dict(b["wraps"], stranger=b["wraps"]["writer"])),
    lambda b: dict(b, participants=[pk("contributor")] * 2),
    lambda b: dict(b, participants=["ab" * 32] * 21),
    lambda b: dict(b, blobs=[{"sha256": "ab" * 32}, {"sha256": "ab" * 32}]),
    lambda b: dict(b, kind="video"),
    lambda b: dict(b, revision=0),
    lambda b: dict(b, revision=True),
    lambda b: dict(b, v=2),
])
def test_a_body_the_server_would_refuse_has_a_problem(change):
    assert submission.submission_body_problem(change(submission_for(sk("writer")).body))


def _blob(index, servers=("https://media.example",)):
    return {"sha256": f"{index:064x}", "size": 10, "servers": list(servers)}


@pytest.mark.parametrize("change, allowed", [
    # Contract 15 (feat/redaktion 5c11475): 64 files per revision, each on
    # at most 10 https servers; a submission-key wrap's ct is exactly 348
    # characters; the client name at most 64.
    (lambda b: dict(b, blobs=[_blob(i) for i in range(64)]), True),
    (lambda b: dict(b, blobs=[_blob(i) for i in range(65)]), False),
    (lambda b: dict(b, blobs=[_blob(1, [f"https://m{i}.example" for i in range(10)])]), True),
    (lambda b: dict(b, blobs=[_blob(1, [f"https://m{i}.example" for i in range(11)])]), False),
    (lambda b: dict(b, blobs=[_blob(1, ["http://media.example"])]), False),
    (lambda b: dict(b, blobs=[{"sha256": "ab" * 32, "servers": []}]), False),
    (lambda b: dict(b, wraps=dict(b["wraps"], writer=dict(b["wraps"]["writer"],
                                                          ct=b["wraps"]["writer"]["ct"] + "A"))),
     False),
    (lambda b: dict(b, client="M" * 64), True),
    (lambda b: dict(b, client="M" * 65), False),
])
def test_the_limits_of_contract_15_are_checked_before_sending(change, allowed):
    problem = submission.submission_body_problem(change(submission_for(sk("writer")).body))
    assert (problem is None) is allowed, problem


def test_a_body_that_cannot_be_built_raises():
    hand = submission_for(sk("writer")).body
    with pytest.raises(FormatError):
        submission.submission_body(
            writer=pk("writer"), publication=hand["publication"],
            key_event_id=hand["key_event_id"], epoch=hand["epoch"], d="text", round_=1,
            revision=1, revision_hash=hand["revision_hash"], kind="article", title="",
            signer="writer", participants=[], p_sub=hand["p_sub"], envelope=hand["envelope"],
            envelope_sha256=hand["envelope_sha256"], wraps=hand["wraps"], blobs=[],
            status_key="ab" * 32, client="MyEditor tests")
