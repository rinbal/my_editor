# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""People who left (editorial contract 10; M-4 item 2): their actions
verify from what the owner and the publication key signed, as the vector
``former_member`` and ``formerStanding`` in the contract's
docs/editorial-fixtures/crosscheck.reference.mjs have it."""

from __future__ import annotations

import json

from nostr import crypto, events, nip98
from nostr.standup import answers, views
from nostr.standup import following as rules
from nostr.standup.former import Formers, parse_formers, standing
from tests.standup_vectors import NOW, SID, VECTORS, fixture_secret

FM = VECTORS["former_member"]


def _standing(former: dict, person: str, epoch):
    found = standing(parse_formers([former])[0], person, epoch, address=FM["publication"])
    return {"member": found.member, "decides": found.decides}


def test_the_standing_of_people_who_left_is_the_vectors():
    assert _standing(FM["taken"], FM["stranger"], 3) == {"member": True, "decides": True}
    for name, former in FM["must_fail"].items():
        assert _standing(former, FM["stranger"], None) == {"member": False,
                                                            "decides": False}, name
    for case in FM["cases"]:
        former = FM["former"][case["former"]]
        assert _standing(former, former["pubkey"], case["epoch"]) == {
            "member": case["member"], "decides": case["decides"]}, case["name"]


def _decision(label: str, revision: int) -> views.TimelineEvent:
    body = {"submission_id": SID, "outcome": "accept", "expected_revision": revision,
            "message_wrap": None, "split_wrap": None, "split_rumor_id": None, "section": None}
    text = json.dumps(body, separators=(",", ":"))
    unsigned = nip98.build_unsigned_auth_event(
        f"https://standup.example/api/submissions/{SID}/decision", "POST", text.encode(),
        now=NOW)
    signer = fixture_secret(label)
    return views.TimelineEvent(
        name="accept", actor=crypto.get_public_key(signer).hex(), revision=revision, to=None,
        signed=views.SignedRecord(event=events.sign_event(unsigned, signer), body=text))


def test_a_decision_by_someone_who_left_verifies_at_the_epoch_of_the_revision_it_names():
    detail = {"former": [FM["former"]["left"], FM["former"]["curator"]]}
    formers = Formers(FM["publication"])
    formers.join(answers.parse_formers(detail["former"]))
    left, curator = _decision("former", 2), _decision("curator", 2)
    assert left.actor == FM["former"]["left"]["pubkey"]
    assert curator.actor == FM["former"]["curator"]["pubkey"]
    checks = rules.SignedChecks()
    # Revision 2 not opened, none after it: nothing is read for it, it waits.
    assert rules.action_verified(left, SID, (), checks, formers) is False
    formers.saw(1, 2)
    formers.saw(3, 3)
    # Revision 2 lies between epochs 2 and 3: the curator decided only at 2.
    assert rules.action_verified(left, SID, (), checks, formers) is True
    assert rules.action_verified(curator, SID, (), checks, formers) is False
    formers.saw(2, 2)
    assert rules.action_verified(curator, SID, (), checks, formers) is True
    assert rules.action_verified(_decision("curator", 3), SID, (), checks, formers) is False
    assert rules.action_verified(left, SID, (), checks) is False      # the server's word alone


def test_a_member_who_left_verifies_taking_a_submission_but_never_a_report():
    # B5 client step 6: taking and handing over need a member (epoch 1);
    # published and placed reports never take this path.
    formers = Formers(FM["publication"])
    formers.join(parse_formers([FM["former"]["curator"]]))
    curator = FM["former"]["curator"]["pubkey"]
    assert formers.decides(curator, "taken", {}, decisions=rules.DECISIONS)
    assert not formers.decides(curator, "published", {}, decisions=rules.DECISIONS)
    assert not formers.decides(curator, "correction", {"epoch": 3}, decisions=rules.DECISIONS)
