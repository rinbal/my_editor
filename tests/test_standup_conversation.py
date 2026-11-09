# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The conversation about a submission (nostr/standup/conversation.py):
rumors, seals and gift wraps built here and read with every check of the
editorial contract (9.2), the shared vectors (every invalid variant
refused), edits, reactions, taking back, structured notes and the size
limits (9.1, 9.3, 9.4; acceptance criterion 9)."""

from __future__ import annotations

import json

import pytest

from nostr import crypto as nostr_crypto
from nostr import events as nostr_events
from nostr.standup import conversation as conv
from nostr.standup import formats, views
from nostr.standup.formats import SecretKeyFound
from tests.standup_fakes import KeySigner
from tests.standup_vectors import NOW, SID, VECTORS, pk, sk

S = VECTORS["seal_wrap"]
ROOM = S["room"]["p_sub"]
ROOM_SK = bytes.fromhex(S["room"]["s_sub"])
EPOCH = S["room"]["epoch_pubkey"]
EPOCH_SK = bytes.fromhex(S["room"]["epoch_sk"])


# -- the shared vectors --------------------------------------------------------------

@pytest.mark.parametrize("name, room_sk", [("message", ROOM_SK), ("reaction", ROOM_SK),
                                           ("internal", EPOCH_SK)])
def test_vectors_open_with_every_check(name, room_sk):
    case = S[name]
    opened = conv.open_item(case["wrap"], room_sk, SID, seq=4)
    assert opened.rumor == case["rumor"]
    assert opened.sender == case["seal"]["pubkey"] == case["rumor"]["pubkey"]
    assert opened.wrap_id == case["wrap"]["id"] and opened.seq == 4


@pytest.mark.parametrize("name", sorted(S["must_fail"]))
def test_vectors_that_must_fail_are_not_shown(name):
    with pytest.raises(conv.NotShown):
        conv.open_item(S["must_fail"][name], ROOM_SK, SID)


def test_the_seal_key_is_the_same_from_either_side():
    from_room = nostr_crypto.conversation_key(ROOM_SK, bytes.fromhex(pk("editor")))
    from_sender = nostr_crypto.conversation_key(sk("editor"), bytes.fromhex(ROOM))
    assert from_room.hex() == S["seal_key_equality"]["from_room"] == from_room.hex()
    assert from_sender.hex() == S["seal_key_equality"]["from_sender"] == from_room.hex()


def test_internal_notes_do_not_open_with_the_submission_key():
    with pytest.raises(conv.NotShown):
        conv.open_item(S["internal"]["wrap"], ROOM_SK, SID)


def test_a_message_of_another_submission_is_not_shown():
    with pytest.raises(conv.NotShown):
        conv.open_item(S["message"]["wrap"], ROOM_SK, "0" * 64)


# -- writing ------------------------------------------------------------------------

def send(rumor, signer=None, room_sk=ROOM_SK, room=ROOM, rand=lambda n: 0):
    signer = signer or KeySigner(sk("writer"))
    got = {}
    conv.seal_and_wrap(signer, rumor, room_sk, room, on_success=lambda w: got.setdefault("ok", w),
                       on_failure=lambda r: got.setdefault("err", r), clock=lambda: NOW,
                       rand=rand)
    return got


def writer_message(text="Hallo Redaktion", **kw):
    return conv.message_rumor(room=ROOM, submission_id=SID, text=text, sender=pk("writer"),
                              created_at=NOW, **kw)


def test_a_message_written_here_reads_back_with_every_check():
    rumor = writer_message()
    signer = KeySigner(sk("writer"))
    wrap = send(rumor, signer)["ok"]
    # The one signer call is the seal, kind 13.
    assert signer.asked == ["sign_event:13"]
    assert wrap["kind"] == 1059 and wrap["tags"] == [["p", ROOM]]
    assert wrap["pubkey"] != pk("writer")
    assert nostr_events.verify_event(wrap)
    opened = conv.open_item(wrap, ROOM_SK, SID)
    assert opened.rumor == rumor and opened.sender == pk("writer")
    assert rumor["tags"] == [["p", ROOM], ["submission", SID]]
    assert "sig" not in rumor
    assert conv.kind_hint(rumor) == "message"


def test_seal_and_wrap_times_are_moved_back_independently():
    draws = iter([1000, 50_000])
    signer = KeySigner(sk("writer"))
    wrap = send(writer_message(), signer, rand=lambda n: next(draws))["ok"]
    seal = signer.signed[0]
    assert seal["created_at"] == NOW - 1000 and wrap["created_at"] == NOW - 50_000
    assert seal["tags"] == []
    assert conv.MAX_BACKDATE_SECONDS == 172_800


def test_the_rumor_keeps_the_real_time():
    wrap = send(writer_message(), rand=lambda n: n - 1)["ok"]
    assert conv.open_item(wrap, ROOM_SK, SID).rumor["created_at"] == NOW


def test_an_internal_note_is_wrapped_to_the_epoch_key():
    rumor = conv.message_rumor(room=EPOCH, submission_id=SID, text="Intern", sender=pk("editor"),
                               created_at=NOW)
    wrap = send(rumor, KeySigner(sk("editor")), room_sk=EPOCH_SK, room=EPOCH)["ok"]
    assert conv.open_item(wrap, EPOCH_SK, SID).rumor["content"] == "Intern"
    with pytest.raises(conv.NotShown):
        conv.open_item(wrap, ROOM_SK, SID)


def test_a_room_key_that_is_not_the_rooms_is_refused_before_signing():
    signer = KeySigner(sk("writer"))
    got = send(writer_message(), signer, room_sk=EPOCH_SK, room=ROOM)
    assert "room" in got["err"] and signer.asked == []


def test_a_signer_that_changes_the_seal_is_not_trusted():
    for tamper in (lambda e: dict(e, content="x"), lambda e: dict(e, sig="00" * 64),
                   lambda e: dict(e, tags=[["p", ROOM]])):
        got = send(writer_message(), KeySigner(sk("writer"), tamper=tamper))
        assert "err" in got and "ok" not in got


def test_a_signer_that_says_no_fails_the_send():
    got = send(writer_message(), KeySigner(sk("writer"), refuse="rejected by user"))
    assert got == {"err": "rejected by user"}


def test_a_seal_signed_by_someone_else_than_the_author_is_not_shown():
    # Criterion 9: the author is the seal's signer, never the server's word.
    rumor = writer_message()
    key = nostr_crypto.conversation_key(ROOM_SK, bytes.fromhex(pk("editor")))
    forged_seal = nostr_events.sign_event(
        {"kind": 13, "tags": [], "created_at": NOW,
         "content": nostr_crypto.encrypt(formats.canonical_json(rumor), key)}, sk("editor"))
    wrap = conv.gift_wrap(forged_seal, ROOM, now=NOW)
    with pytest.raises(conv.NotShown, match="author"):
        conv.open_item(wrap, ROOM_SK, SID)


@pytest.mark.parametrize("change", [
    lambda w: dict(w, sig="00" * 64),
    lambda w: dict(w, kind=1),
    lambda w: dict(w, content=w["content"][:-4] + "AAAA"),
])
def test_a_damaged_gift_wrap_is_not_shown(change):
    wrap = send(writer_message())["ok"]
    with pytest.raises(conv.NotShown):
        conv.open_item(change(wrap), ROOM_SK, SID)


def test_a_wrap_for_another_room_is_not_shown():
    signer = KeySigner(sk("writer"))
    rumor = writer_message()
    seal = conv.seal_unsigned(rumor, ROOM_SK, now=NOW)
    signed = nostr_events.sign_event(dict(seal), sk("writer"))
    wrap = conv.gift_wrap(signed, EPOCH, now=NOW)        # wrapped to the wrong key
    with pytest.raises(conv.NotShown):
        conv.open_item(wrap, ROOM_SK, SID)
    del signer


def test_a_seal_with_tags_other_than_expiration_is_not_shown():
    rumor = writer_message()
    for tags, shown in (([["expiration", str(NOW + 99)]], True), ([["p", ROOM]], False)):
        seal = dict(conv.seal_unsigned(rumor, ROOM_SK, now=NOW), tags=tags)
        wrap = conv.gift_wrap(nostr_events.sign_event(seal, sk("writer")), ROOM, now=NOW)
        if shown:
            assert conv.open_item(wrap, ROOM_SK, SID).rumor == rumor
        else:
            with pytest.raises(conv.NotShown):
                conv.open_item(wrap, ROOM_SK, SID)


def test_a_rumor_whose_id_does_not_match_is_not_shown():
    rumor = dict(writer_message(), content="changed after the id")
    seal = nostr_events.sign_event(conv.seal_unsigned(rumor, ROOM_SK, now=NOW), sk("writer"))
    with pytest.raises(conv.NotShown, match="id"):
        conv.open_item(conv.gift_wrap(seal, ROOM, now=NOW), ROOM_SK, SID)


# -- limits and secret keys (9.1, 5) ----------------------------------------------------------

def test_a_message_at_the_length_limit_is_sent():
    rumor = writer_message("ä" * formats.MAX_MESSAGE_CHARS)
    assert conv.rumor_size(rumor) <= formats.MAX_RUMOR_BYTES
    wrap = send(rumor)["ok"]
    assert len(formats.canonical_bytes(wrap)) <= formats.MAX_WRAP_EVENT_BYTES
    assert conv.open_item(wrap, ROOM_SK, SID).rumor["content"] == "ä" * 16_000


def test_too_many_characters_are_refused():
    with pytest.raises(conv.MessageRefused):
        writer_message("x" * (formats.MAX_MESSAGE_CHARS + 1))


def test_a_rumor_over_32768_bytes_is_refused_even_within_the_characters():
    # 16,000 characters that each escape to six bytes in JSON.
    with pytest.raises(conv.MessageRefused, match="32768"):
        writer_message("\x01" * 16_000)


def test_an_empty_message_is_refused():
    with pytest.raises(conv.MessageRefused):
        writer_message("   ")


def test_a_message_with_a_secret_key_is_refused():
    with pytest.raises(SecretKeyFound):
        writer_message("hier: nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5")
    with pytest.raises(SecretKeyFound):
        writer_message("my private key " + "ab" * 32)


# -- what a conversation shows (9.3, 9.4) -------------------------------------------------------

def opened(rumor, seq, sender=None):
    return conv.Opened(rumor=rumor, sender=sender or rumor["pubkey"], wrap_id=f"w{seq}", seq=seq)


def editor_message(text, at=NOW):
    return conv.message_rumor(room=ROOM, submission_id=SID, text=text, sender=pk("editor"),
                              created_at=at)


def react(target, emoji, who="writer", at=NOW):
    return conv.reaction_rumor(room=ROOM, submission_id=SID, target_id=target["id"],
                               target_author=target["pubkey"], emoji=emoji, sender=pk(who),
                               created_at=at)


def take_back(reaction, who="writer", at=NOW):
    return conv.retraction_rumor(room=ROOM, submission_id=SID, reaction_id=reaction["id"],
                                 sender=pk(who), created_at=at)


def test_the_vector_reaction_lands_on_its_message():
    items = [conv.open_item(S["message"]["wrap"], ROOM_SK, SID, seq=1),
             conv.open_item(S["reaction"]["wrap"], ROOM_SK, SID, seq=2)]
    bubbles = conv.fold(items)
    assert [b.id for b in bubbles] == [S["message"]["rumor"]["id"]]
    assert bubbles[0].reactions == {pk("writer"): "❤️"}


def test_a_reaction_replaces_the_same_persons_earlier_one():
    first = editor_message("Bitte einen Absatz mehr")
    heart = react(first, "❤️", at=NOW + 1)
    thumbs = react(first, "\U0001f44d", at=NOW + 2)
    other = react(first, "❓", who="contributor", at=NOW + 3)
    bubbles = conv.fold([opened(first, 1), opened(heart, 2), opened(thumbs, 3), opened(other, 4)])
    assert bubbles[0].reactions == {pk("writer"): "\U0001f44d", pk("contributor"): "❓"}
    assert conv.kind_hint(heart) == "reaction"


def test_only_the_reactions_author_takes_it_back():
    first = editor_message("x")
    heart = react(first, "❤️")
    by_someone_else = take_back(heart, who="editor", at=NOW + 1)
    assert conv.fold([opened(first, 1), opened(heart, 2), opened(by_someone_else, 3)])[
        0].reactions == {pk("writer"): "❤️"}
    by_author = take_back(heart, at=NOW + 2)
    assert conv.fold([opened(first, 1), opened(heart, 2), opened(by_someone_else, 3),
                      opened(by_author, 4)])[0].reactions == {}
    assert conv.kind_hint(by_author) == "retraction"


def test_reacting_again_after_taking_back_counts():
    first = editor_message("x")
    heart = react(first, "❤️", at=NOW + 1)
    again = react(first, "\U0001f602", at=NOW + 3)
    bubbles = conv.fold([opened(first, 1), opened(heart, 2), opened(take_back(heart, at=NOW + 2), 3),
                         opened(again, 4)])
    assert bubbles[0].reactions == {pk("writer"): "\U0001f602"}


def test_an_unknown_reaction_is_not_one_of_the_six():
    with pytest.raises(conv.MessageRefused):
        react(editor_message("x"), "\U0001f525")


def test_the_newest_edit_by_the_author_wins_and_others_are_ignored():
    first = editor_message("Erste Fassung", at=NOW)
    edit1 = conv.message_rumor(room=ROOM, submission_id=SID, text="Zweite", sender=pk("editor"),
                               created_at=NOW + 10, edit_of=first["id"])
    edit2 = conv.message_rumor(room=ROOM, submission_id=SID, text="Dritte", sender=pk("editor"),
                               created_at=NOW + 20, edit_of=first["id"])
    foreign = conv.message_rumor(room=ROOM, submission_id=SID, text="Fremd", sender=pk("writer"),
                                 created_at=NOW + 30, edit_of=first["id"])
    bubbles = conv.fold([opened(first, 1), opened(edit2, 3), opened(edit1, 2), opened(foreign, 4)])
    assert len(bubbles) == 1
    assert bubbles[0].text == "Dritte" and bubbles[0].edited
    assert conv.kind_hint(edit1) == "edit"


def test_order_is_the_servers_seq_not_the_rumors_time():
    late = editor_message("später geschrieben", at=NOW + 500)
    early = editor_message("früher geschrieben", at=NOW)
    bubbles = conv.fold([opened(early, 9), opened(late, 2)])
    assert [b.text for b in bubbles] == ["später geschrieben", "früher geschrieben"]


def test_structured_notes_read_back():
    content = conv.split_proposal_content(
        "ab" * 32, [{"pubkey": pk("writer"), "weight": 80, "role": "writer"},
                    {"pubkey": pk("publication"), "weight": 20, "role": "publication"}])
    rumor = conv.structured_rumor(room=ROOM, submission_id=SID, kind="split-proposal",
                                  content=content, sender=pk("editor"), created_at=NOW)
    note = conv.structured_rumor(room=ROOM, submission_id=SID, kind="revision-note",
                                 content=conv.revision_note_content(2, "Absatz ergänzt"),
                                 sender=pk("writer"), created_at=NOW)
    assert rumor["tags"][-1] == ["standup", "split-proposal"]
    assert json.loads(rumor["content"]) == content
    wrap = send(rumor, KeySigner(sk("editor")))["ok"]
    bubbles = conv.fold([conv.open_item(wrap, ROOM_SK, SID, seq=1), opened(note, 2)])
    assert bubbles[0].structured == ("split-proposal", content)
    assert bubbles[1].structured == ("revision-note", {"v": 1, "revision": 2,
                                                       "note": "Absatz ergänzt"})


@pytest.mark.parametrize("shares", [
    [{"pubkey": "ff" * 32, "weight": 100, "role": "writer"}],                       # not a key
    [{"pubkey": pk("writer"), "weight": 90, "role": "writer"}],                     # not 100
    [{"pubkey": pk("writer"), "weight": 100, "role": "boss"}],                      # no such role
    [{"pubkey": pk("writer"), "weight": 50.5, "role": "writer"}],                   # not whole
])
def test_a_split_off_the_contract_is_refused(shares):
    with pytest.raises(conv.MessageRefused):
        conv.split_proposal_content("ab" * 32, shares)


def test_an_unknown_structured_note_is_left_out():
    rumor = conv.build_rumor(14, "{}", [["p", ROOM], ["submission", SID], ["standup", "vote"]],
                             pk("editor"), NOW)
    assert conv.fold([opened(rumor, 1)]) == []


def test_open_items_counts_what_fails_and_sorts_by_seq():
    good = send(writer_message("eins"))["ok"]

    def message(seq, wrap):
        return {"seq": seq, "at": NOW, "type": "message", "wrap": wrap, "event": None}

    items = views.parse_timeline([
        message(5, S["message"]["wrap"]), message(2, good),
        message(3, S["must_fail"]["pubkey_mismatch"]),
        {"seq": 6, "at": NOW, "type": "event", "wrap": None,
         "event": {"name": "taken", "actor": pk("editor"), "revision": None, "to": None,
                   "signed": None}}], internal=False)
    timeline = conv.open_items(items, ROOM_SK, SID)
    assert [o.seq for o in timeline.opened] == [2, 5]
    assert timeline.not_shown == 1
