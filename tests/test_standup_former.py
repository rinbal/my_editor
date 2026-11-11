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
from tests.standup_vectors import SID, VECTORS, fixture_secret

FM = VECTORS["former_member"]


def _standing(former: dict, person: str, epoch, at):
    found = standing(parse_formers([former])[0], person, epoch, at, address=FM["publication"])
    return {"member": found.member, "decides": found.decides}


def test_the_standing_of_people_who_left_is_the_vectors():
    # Contract 569b3f8: membership by epoch, the role by time, an owner key
    # event's too (it counts as redaktion from when it was signed).
    acted = FM["acted_at"]
    assert _standing(FM["taken"], FM["stranger"], 3, acted) == {"member": True, "decides": True}
    for name, former in FM["must_fail"].items():
        assert _standing(former, FM["stranger"], None, acted) == {"member": False,
                                                                   "decides": False}, name
    for case in FM["cases"]:
        former = FM["former"][case["former"]]
        assert _standing(former, former["pubkey"], case["epoch"], case["at"]) == {
            "member": case["member"], "decides": case["decides"]}, case["name"]
    for case in FM["served_cases"]:
        former = case["former"]
        assert _standing(former, former["pubkey"], case["epoch"], case["at"]) == {
            "member": case["member"], "decides": case["decides"]}, case["name"]


def _decision(label: str, revision: int, at: int) -> views.TimelineEvent:
    body = {"submission_id": SID, "outcome": "accept", "expected_revision": revision,
            "message_wrap": None, "split_wrap": None, "split_rumor_id": None, "section": None}
    text = json.dumps(body, separators=(",", ":"))
    unsigned = nip98.build_unsigned_auth_event(
        f"https://standup.example/api/submissions/{SID}/decision", "POST", text.encode(),
        now=at)
    signer = fixture_secret(label)
    return views.TimelineEvent(
        name="accept", actor=crypto.get_public_key(signer).hex(), revision=revision, to=None,
        signed=views.SignedRecord(event=events.sign_event(unsigned, signer), body=text))


def test_a_decision_by_someone_who_left_goes_by_the_role_they_held_when_they_signed():
    # Contract 53ff043: checked at the current revision's epoch first; only
    # when that fails is its own revision read (once) and checked at its
    # epoch. The role is the one of the invitation signed last before it.
    asked = []
    formers = Formers(FM["publication"], asked.append)
    formers.join(answers.parse_formers([FM["former"]["left"], FM["former"]["curator"]]))
    checks = rules.SignedChecks()
    left = _decision("former", 2, 1762673400)
    assert left.actor == FM["former"]["left"]["pubkey"]
    formers.saw(3, 3)                   # the current revision, at epoch 3
    assert rules.action_verified(left, SID, (), checks, formers) is True and asked == []
    formers.epochs.clear()
    formers.saw(3, 4)                   # they left before epoch 4
    assert rules.action_verified(left, SID, (), checks, formers) is False
    assert rules.action_verified(left, SID, (), checks, formers) is False and asked == [2]
    formers.saw(2, 3)                   # revision 2 read: its own epoch
    assert rules.action_verified(left, SID, (), checks, formers) is True
    # Invited again as a curator before deciding: decides nothing, at any epoch.
    curator = _decision("curator", 2, 1762673200)
    assert rules.action_verified(curator, SID, (), checks, formers) is False
    assert rules.action_verified(_decision("curator", 2, 1762671200), SID, (), checks,
                                 formers) is True           # before, as redaktion
    assert rules.action_verified(left, SID, (), checks) is False      # the server's word alone


def test_a_member_who_left_verifies_taking_a_submission_but_never_a_report():
    # B5 client step 6: taking and handing over need a member (epoch 1);
    # published and placed reports never take this path.
    formers = Formers(FM["publication"])
    formers.join(parse_formers([FM["former"]["curator"]]))
    curator, at = FM["former"]["curator"]["pubkey"], FM["acted_at"]
    assert formers.decides(curator, "taken", {}, decisions=rules.DECISIONS, at=at)
    assert not formers.decides(curator, "published", {}, decisions=rules.DECISIONS, at=at)
    assert not formers.decides(curator, "correction", {"epoch": 3}, decisions=rules.DECISIONS,
                               at=at)
