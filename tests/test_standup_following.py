# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Following a submission (nostr/standup/following.py, thread.py and
nostr/ui/standup_following.py, standup_talk.py) against the checking
fake server, with the account's real list and read state.

Pinned here: one status word, section and turn for every state and role
(criterion 6); rows from the list's entries joined with the lookup and
the server's Views, an entry without a receipt as an outbox row, a
request without an answer resolved after a restart; unread from
``last_message_seq``; the newest decision named only once it verified;
no internal note and no forged sender ever shown (criteria 8, 9); a
message's round trip with its failure and retry; reactions (criterion
10); withdraw; and that a stopped account writes nothing more (19)."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from PySide6.QtWidgets import QApplication

from nostr.standup import conversation, following as rules, thread as threads, views
from nostr.standup import crypto as scrypto
from nostr.standup.errors import Code
from nostr.standup.following import Action, Section, Situation
from nostr.ui.standup_talk import Sending, footer_words, shown_reactions
from tests.standup_fakes import DROP_AFTER, DROP_BEFORE, submission_for
from tests.standup_following_fakes import ADDRESS, FollowWorld, entry_of, room_wrap
from tests.standup_vectors import NOW, pk, sk

ME, EDITOR, OTHER = pk("writer"), pk("editor"), pk("contributor")


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def names(pubkey: str) -> str:
    return {EDITOR: "Anna", ME: "Jana", OTHER: "Paul"}.get(pubkey, "")


def view_of(state, *, step=None, turn="none", pending=(), writer=ME, placed=False,
            responsible=None, revision=1, last_seq=1, last_message_seq=1, section=None):
    return views.parse_view({
        "submission_id": "ab" * 32, "publication": ADDRESS, "kind": "article",
        "title": "Ein Jahr", "writer": writer, "participants": [OTHER], "state": state,
        "step": step, "turn": {"type": turn, "pubkey": None, "pending": list(pending)},
        "responsible": responsible, "marks": [], "signer": "writer", "section": section,
        "revision": revision, "round": 1, "p_sub": "cd" * 32, "epoch": 3,
        "revision_hash": "ef" * 32, "created_at": NOW, "updated_at": NOW, "last_seq": last_seq,
        "last_message_seq": last_message_seq, "confirmations": {}, "published": None,
        "placed": placed, "proposal_by": None, "later_round": False})


# -- one word, one section, whose turn (criterion 6) ----------------------------------

def test_every_state_and_role_has_one_word_one_section_and_whose_turn():
    # Criterion 6, as one table: the View, who looks, and what they see.
    for view, me, situation, section, word in [
    (view_of("submitted", turn="redaktion"), ME, Situation.SUBMITTED, Section.RUNNING,
     "Submitted"),
    (view_of("in_review", turn="editor", responsible=EDITOR), ME, Situation.IN_REVIEW,
     Section.RUNNING, "Anna is reviewing"),
    (view_of("in_review", turn="redaktion"), ME, Situation.IN_REVIEW, Section.RUNNING,
     "In review"),
    (view_of("changes_requested", turn="writer"), ME, Situation.CHANGES_REQUESTED,
     Section.TURN, "Changes requested"),
    (view_of("changes_requested", turn="writer"), OTHER, Situation.CHANGES_BY_WRITER,
     Section.RUNNING, "Changes requested"),
    (view_of("accepted", step="confirm", turn="contributors", pending=[ME, OTHER]), ME,
     Situation.CONFIRM, Section.TURN, "Please confirm"),
    (view_of("accepted", step="confirm", turn="contributors", pending=[OTHER]), ME,
     Situation.ACCEPTED, Section.RUNNING, "Accepted"),
    (view_of("accepted", step="confirm", turn="contributors", pending=[OTHER]), OTHER,
     Situation.CONFIRM, Section.TURN, "Please confirm"),
    (view_of("accepted", step="writer_publishes", turn="writer"), ME, Situation.PUBLISH,
     Section.TURN, "Accepted"),
    (view_of("accepted", step="writer_publishes", turn="writer"), OTHER, Situation.ACCEPTED,
     Section.RUNNING, "Accepted"),
    (view_of("accepted", step="ready_for_release", turn="releaser"), ME, Situation.ACCEPTED,
     Section.RUNNING, "Accepted"),
    (view_of("published", turn="releaser"), ME, Situation.PUBLISHED_UNPLACED, Section.RUNNING,
     "Published, place to follow"),
    (view_of("published", turn="writer", placed=True), ME, Situation.CORRECTION, Section.TURN,
     "Correction suggested"),
    (view_of("published", placed=True), ME, Situation.PUBLISHED, Section.DONE, "Published"),
    (view_of("declined"), ME, Situation.DECLINED, Section.DONE, "Declined"),
    (view_of("withdrawn"), ME, Situation.WITHDRAWN, Section.DONE, "Withdrawn"),
]:
        assert rules.situation_of(view, me) is situation
        row = rules.Row(sid=view.submission_id, title=view.title, publication=ADDRESS,
                        kind="article", role="writer" if me == ME else "contributor",
                        writer=ME, d="", situation=situation, updated_at=int(NOW), view=view)
        assert row.section is section and row.turn is rules.my_turn(view, me)
        assert rules.status_word(row, names) == word
        assert rules.pill_word(row, names) == ("Your turn" if row.turn else word)


def test_the_table_has_a_row_for_every_situation_and_at_most_one_action():
    assert set(rules.TABLE) == set(Situation)
    assert {rules.TABLE[s].action for s in (Situation.PUBLISH, Situation.CONFIRM,
                                           Situation.CORRECTION)} == {
        Action.PUBLISH, Action.CONFIRM, Action.COMPARE_CORRECTION}      # M-4's seams


# -- the rows: entries, the lookup, the Views ------------------------------------------

def situations(following):
    return {row.d or row.sid: row.situation for row in following.rows()}


def test_a_stored_submission_is_submitted_once_its_receipt_confirms_the_entry():
    world = FollowWorld()
    account = world.account()
    world.submit(account, d="in")
    following = world.following(account)
    assert situations(following) == {"in": Situation.SUBMITTED}
    # The lookup carries no signature (plan 10.4).
    lookups = [h for _v, path, _b, h in world.server.requests
               if path == "/api/submissions/lookup"]
    assert lookups and all(h["Authorization"] is None for h in lookups)


def test_an_entry_the_server_has_no_receipt_for_is_an_outbox_row_and_can_be_discarded():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="nie", stored=False, send=False)
    following = world.following(account)
    row = following.rows()[0]
    assert row.situation is Situation.UNSENT and row.turn and not row.unread
    status = rules.status_of(row, publication="Magazin", name_of=names)
    assert status.action is Action.RESEND and status.sentence == "Not sent."
    done = []
    following.discard(entry.sid, done.append)
    world.settle()
    assert done == [True] and following.rows() == []
    # A keyed tombstone: the record of these keys, nothing else (13).
    assert world.discarded[0].sid == entry.sid


def test_an_entry_is_never_called_not_sent_before_the_server_answered():
    world = FollowWorld()
    account = world.account()
    world.submit(account, d="offen", stored=False, send=False)
    world.submit(account, d="war-da", stored=True, send=False)
    world.server.inject("lookup", DROP_BEFORE)
    following = world.following(account)
    assert situations(following) == {"offen": Situation.UNCONFIRMED,
                                     "war-da": Situation.UNKNOWN}


def test_a_request_that_got_no_answer_is_resolved_after_a_restart():
    # 12.4: the entry was written before sending; the answer never came
    # and the app was closed. The next start asks by the status key.
    world = FollowWorld()
    first = world.account()
    _prepared, entry = world.submit(first, d="unbestaetigt", stored=False)
    assert not first.submissions.entry(entry.sid).stored
    again = world.account()                     # the same person, started again
    following = world.following(again)
    assert situations(following) == {"unbestaetigt": Situation.SUBMITTED}
    assert again.submissions.entry(entry.sid).stored
    third = world.account()                     # and the list on the relays says so
    world.following(third)
    assert third.submissions.entry(entry.sid).stored


def test_a_round_another_device_stored_says_so_and_reloads():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="doppelt", stored=False, send=False)
    following = world.following(account, start=False)
    following.marks.refuse_round(account.writer, entry)
    following.start()
    world.settle()
    row = following.rows()[0]
    assert row.situation is Situation.ELSEWHERE
    assert rules.status_of(row, publication="M", name_of=names).action is Action.RELOAD


def test_a_view_the_server_gives_never_makes_an_entry_without_a_receipt_submitted():
    # Lessons 16, 32: another device stored this round with its own
    # secrets. The server lists the round by its sid, so its View alone
    # must not turn this device's entry, which has no receipt, into
    # "Eingereicht", nor offer a composer with a key that is not the room's.
    world = FollowWorld()
    account = world.account()
    world.submit(account, d="doppelt", listed=False)            # the other device's
    _prepared, entry = world.submit(account, d="doppelt", stored=False, send=False)
    following = world.following(account, start=False)
    following.marks.refuse_round(account.writer, entry)
    following.view_opened()                                     # reads the server's Views
    world.settle()
    row = following.rows()[0]
    assert row.situation is Situation.ELSEWHERE and row.view is None and not row.open
    following.open(entry.sid)
    world.settle()
    assert not following.detail(entry.sid).can_write
    # The lookup is down and the entry was never stored: not confirmed,
    # whatever the server's list of the person's works says.
    served = {entry.sid: view_of("submitted")}
    rows = rules.rows_of([entry], me=ME, followed={}, served=served, unknown=set(),
                         running={}, refused=lambda e: False, seen=lambda sid: 0)
    assert rows[0].situation is Situation.UNCONFIRMED and not rows[0].confirmed
    # One stored before is refined by the View, but "Eingereicht" stays
    # the receipt's word.
    stored = replace(entry, stored=True)
    for state, situation in (("submitted", Situation.UNKNOWN),
                             ("changes_requested", Situation.CHANGES_REQUESTED)):
        served = {entry.sid: view_of(state, turn="writer")}
        rows = rules.rows_of([stored], me=ME, followed={}, served=served, unknown=set(),
                             running={}, refused=lambda e: False, seen=lambda sid: 0)
        assert rows[0].situation is situation


def test_a_picture_the_server_misses_is_never_called_submitted():
    world = FollowWorld()
    account = world.account()
    blob = {"sha256": "aa" * 32, "size": 10, "servers": ["https://media.example"]}
    prepared = submission_for(sk("writer"), d="bild", blobs=(blob,))
    account.api.submit(prepared.body, lambda _r: None, lambda _e: None)
    world.settle()
    account.submissions.record(entry_of(prepared, d="bild"), lambda _r: None)
    world.settle()
    following = world.following(account)
    assert situations(following) == {"bild": Situation.PICTURES_MISSING}


def test_a_list_that_does_not_load_is_asked_again_quietly_and_said_once():
    world = FollowWorld()
    account = world.account()
    world.submit(account, d="da")
    for _ in range(4):
        world.server.inject("lookup", DROP_BEFORE)
    following = world.following(account)
    assert following.problem == ""                      # the first try failed: nothing said
    waits = []
    while world.later.waiting:
        waits += world.later.run()
        world.settle()
    assert waits == [2_000, 6_000, 15_000]
    assert following.problem == "Check your internet connection and try again."
    following.refresh()                                 # Erneut versuchen
    world.settle()
    assert following.problem == "" and situations(following) == {"da": Situation.SUBMITTED}


def test_unread_comes_from_messages_never_from_reactions():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="lesen")
    following = world.following(account)
    following.open(entry.sid)                           # read up to now
    world.settle()
    assert not following.rows()[0].unread
    world.message(prepared, "Bitte kürzen")
    following.refresh()
    world.settle()
    assert following.rows()[0].unread
    following.open(entry.sid)
    world.settle()
    assert not following.rows()[0].unread
    rumor = conversation.reaction_rumor(
        room=scrypto.pubkey_of(prepared.s_sub), submission_id=entry.sid,
        target_id="11" * 32, target_author=ME, emoji=conversation.REACTIONS[1],
        sender=EDITOR, created_at=int(NOW))
    world.post(prepared, room_wrap(prepared, rumor, "editor"), kind_hint="reaction")
    following.refresh()
    world.settle()
    row = following.rows()[0]
    assert row.view.last_seq > row.view.last_message_seq and not row.unread


def test_contributors_see_the_works_they_are_named_in_once_the_view_opens():
    world = FollowWorld()
    writer = world.account()
    world.submit(writer, d="gemeinsam", participants=(OTHER,))
    paul = world.account("contributor")
    following = world.following(paul)
    assert following.rows() == []                       # launch asks no signer
    assert paul.sign.requests == []
    following.view_opened()
    world.settle()
    row = following.rows()[0]
    assert (row.role, row.writer, row.situation) == ("contributor", ME, Situation.SUBMITTED)


# -- the notice at the article ------------------------------------------------------------

def test_news_is_a_message_of_someone_else_or_a_result_once():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="neu")
    following = world.following(account)
    following.open(entry.sid)
    world.settle()
    world.news.clear()
    world.message(prepared, "Kannst du kürzen?")
    following.refresh()
    world.settle()
    assert [(n.kind, n.d) for n in world.news] == [("message", "neu")]
    following.refresh()
    world.settle()
    assert len(world.news) == 1                         # once per seq
    world.decide(prepared, "request_changes", text="Bitte einen Absatz mehr.")
    following.refresh()
    world.settle()
    assert world.news[-1].kind == "request_changes"


# -- the detail -----------------------------------------------------------------------------

def opened(world, following, entry):
    following.open(entry.sid)
    world.settle()
    return following.detail(entry.sid)


def test_the_sentence_names_the_editor_only_once_the_decision_verified():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="aendern")
    world.decide(prepared, "request_changes", text="Bitte einen Absatz mehr.")
    following = world.following(account)
    detail = opened(world, following, entry)
    assert detail.decision == rules.Decision("request_changes", EDITOR, True)
    status = rules.status_of(detail.row, publication=detail.publication, name_of=names,
                             decision=detail.decision)
    assert (status.sentence, status.action, status.unsigned) == (
        "Anna asks for changes.", Action.REVISE, False)
    # The text in the editor differs: the next version can be handed in.
    edited = rules.status_of(detail.row, publication="M", name_of=names,
                             decision=detail.decision, edited=True)
    assert (edited.sentence, edited.more, edited.action) == (
        "Your changes are in the editor.", "Hand them in as version 2 when you’re ready.",
        Action.HAND_IN)
    captions = [threads.caption_text(c, me=ME, name_of=names) for c in detail.thread
                if isinstance(c, threads.Caption)]
    assert captions == ["Submitted · Version 1", "Anna asked for changes"]


def test_a_decision_that_does_not_verify_is_said_as_reported_with_more_about_it():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="unsigniert")
    world.decide(prepared, "request_changes", text="Bitte.")
    # The server serves the decision, signed by someone who may not decide.
    world.server.publication().members.pop(EDITOR)
    following = world.following(account)
    detail = opened(world, following, entry)
    assert detail.decision.verified is False
    status = rules.status_of(detail.row, publication="M", name_of=names,
                             decision=detail.decision, complete=detail.complete)
    assert status.sentence == "The editorial team asks for changes." and status.unsigned
    caption = [c for c in detail.thread if isinstance(c, threads.Caption)][-1]
    assert threads.caption_text(caption, me=ME, name_of=names) == "Changes requested"
    assert threads.unsigned(caption)


def test_a_sentence_rests_on_the_newest_verified_decision_or_says_that_it_does_not():
    # Contract 10, from the one table: what a situation rests on, and
    # what is said when the newest decision is missing, another one or
    # not verified. Nothing is said while the submission is not read.
    resting = {s: w.decision for s, w in rules.TABLE.items() if w.decision}
    assert resting == {
        Situation.CHANGES_REQUESTED: "request_changes",
        Situation.CHANGES_BY_WRITER: "request_changes", Situation.CONFIRM: "accept",
        Situation.ACCEPTED: "accept", Situation.PUBLISH: "accept",
        Situation.PUBLISHED_UNPLACED: "accept", Situation.PUBLISHED: "accept",
        Situation.DECLINED: "decline"}
    for situation, required in resting.items():
        row = rules.Row(sid="ab" * 32, title="Ein Jahr", publication=ADDRESS, kind="article",
                        role="writer", writer=ME, d="", situation=situation,
                        updated_at=int(NOW), view=view_of("declined"), confirmed=True)

        def status(decision, complete=True):
            return rules.status_of(row, publication="M", name_of=names, decision=decision,
                                   complete=complete)

        other = "decline" if required != "decline" else "accept"
        assert not status(rules.Decision(required, EDITOR, True)).unsigned
        assert status(None).unsigned                                    # left out
        assert status(rules.Decision(other, EDITOR, True)).unsigned     # another one
        assert status(rules.Decision(required, EDITOR, False)).unsigned
        assert not status(None, complete=False).unsigned                # not read yet
        # What acts on the Redaktion's word waits for that word.
        action = rules.TABLE[situation].action
        if action in rules.ON_A_DECISION:
            assert status(None).action is None
            assert status(rules.Decision(required, EDITOR, True)).action is action
    # A server that reports "declined" and leaves the decision out.
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="ohne")
    world.decide(prepared, "decline")
    record = world.record(entry.sid)
    record.timeline = [i for i in record.timeline
                       if (i.get("event") or {}).get("name") != "decline"]
    detail = opened(world, world.following(account), entry)
    assert detail.row.situation is Situation.DECLINED and detail.complete
    said = rules.status_of(detail.row, publication="M", name_of=names,
                           decision=detail.decision, complete=detail.complete)
    assert said.sentence == "Declined." and said.unsigned


def test_internal_notes_and_forged_senders_are_never_shown():
    # Criteria 8 and 9, with a writer who is a member too: the server
    # hands them internal notes, and the writer's side shows none.
    world = FollowWorld()
    account = world.account()
    world.server.publication().members[ME] = "redaktion"
    prepared, entry = world.submit(account, d="intern")
    world.message(prepared, "Gut so.")
    # Sealed by the editor around a rumor that names the writer (17.md).
    world.message(prepared, "Gefälscht", forge_author=ME)
    note = room_wrap(prepared, conversation.message_rumor(
        room=scrypto.pubkey_of(prepared.s_sub), submission_id=entry.sid,
        text="Nur für die Redaktion", sender=EDITOR, created_at=int(NOW)), "editor")
    world.server._append(world.record(entry.sid),
                         {"type": "internal", "wrap": note, "event": None})
    served = []
    account.api.submission(entry.sid, served.append, served.append)
    world.settle()
    assert len(served[0].internal) == 1 and len(served[0].timeline) >= 4
    following = world.following(account)
    detail = opened(world, following, entry)
    assert [m.bubble.text for m in detail.thread
            if isinstance(m, threads.Message)] == ["Gut so."]


def test_a_sender_is_called_by_their_role_and_a_stranger_is_never_the_redaktion():
    # The server cannot read a seal: whoever holds the room key can post
    # under any key. A contributor posts once as themselves and once
    # under a throwaway key, in the Redaktion's tone.
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="rollen", participants=(OTHER,))
    room = scrypto.pubkey_of(prepared.s_sub)
    world.message(prepared, "Bitte kürzen.")                        # the editor
    world.message(prepared, "Ich schaue drüber.", sender="contributor")
    forged = conversation.message_rumor(
        room=room, submission_id=entry.sid, text="Die Redaktion: Bitte neu einreichen.",
        sender=pk("recipient"), created_at=int(NOW))
    world.post(prepared, room_wrap(prepared, forged, "recipient"), sender="contributor")
    following = world.following(account)
    # Before the member list is read nobody is called the Redaktion, in
    # the list's newest message or the notice.
    sender, role, text = following.snippet(entry.sid)
    assert (sender, role, text) == (pk("recipient"), threads.Role.UNKNOWN,
                                    "Die Redaktion: Bitte neu einreichen.")
    assert threads.sender_label(role, "") == ""
    assert world.news[-1].kind == "message" and world.news[-1].role is threads.Role.UNKNOWN
    detail = opened(world, following, entry)
    shown = [(m.bubble.text, m.role, threads.sender_label(m.role, ""))
             for m in detail.thread if isinstance(m, threads.Message)]
    assert shown == [("Bitte kürzen.", threads.Role.MEMBER, "Editorial team"),
                     ("Ich schaue drüber.", threads.Role.PARTICIPANT, "Contributor")]
    assert following.snippet(entry.sid) == (OTHER, threads.Role.PARTICIPANT,
                                            "Ich schaue drüber.")
    assert threads.sender_label(threads.Role.PARTICIPANT, "Paul") == "Paul"


def test_a_message_goes_out_shows_sending_and_is_a_bubble_once_it_has_its_seq():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="schreiben")
    following = world.following(account)
    opened(world, following, entry)
    account.signer.held = []                            # the signer app is asked
    assert following.talk.send(entry.sid, "Mach ich, danke!") == ""
    [out] = following.talk.outgoing(entry.sid)
    assert out.state is Sending.SIGNING and footer_words(out) == ("Sending …", "")
    account.signer.release()
    world.settle()
    detail = following.detail(entry.sid)
    assert detail.outgoing == ()
    [message] = [m for m in detail.thread if isinstance(m, threads.Message)]
    assert message.mine and message.bubble.text == "Mach ich, danke!" and message.bubble.seq
    assert not following.rows()[0].unread               # one's own message is read


def test_a_message_that_did_not_go_keeps_its_text_and_goes_with_one_retry():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="offline")
    following = world.following(account)
    opened(world, following, entry)
    for _ in range(4):
        world.server.inject("messages", DROP_BEFORE)
    following.talk.send(entry.sid, "Hallo?")
    world.settle()
    while world.later.waiting:                          # the quiet tries
        world.later.run()
        world.settle()
    [out] = following.talk.outgoing(entry.sid)
    assert out.state is Sending.FAILED and out.text == "Hallo?"
    assert footer_words(out) == (
        "Not sent. Check your internet connection and try again.", "Send Again")
    signed = len(account.key.signed)
    following.talk.retry(out.key)
    world.settle()
    detail = following.detail(entry.sid)
    assert [m.bubble.text for m in detail.thread if isinstance(m, threads.Message)] == ["Hallo?"]
    assert len(account.key.signed) == signed            # the same wrap: no new prompt


def test_a_message_that_may_have_arrived_is_checked_and_never_sent_twice():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="vielleicht")
    following = world.following(account)
    opened(world, following, entry)
    world.server.inject("messages", DROP_AFTER)         # stored, the answer lost
    following.talk.send(entry.sid, "Angekommen?")
    world.settle()
    [out] = following.talk.outgoing(entry.sid)
    assert out.state is Sending.UNCONFIRMED
    assert footer_words(out) == ("Not confirmed yet.", "Check Again")
    following.talk.retry(out.key)                       # Erneut prüfen
    world.settle()
    detail = following.detail(entry.sid)
    assert detail.outgoing == ()
    assert [m.bubble.text for m in detail.thread
            if isinstance(m, threads.Message)] == ["Angekommen?"]
    posts = [path for verb, path, _b, _h in world.server.requests
             if verb == "POST" and path.endswith("/messages")]
    assert len(posts) == 1


def test_a_message_with_a_secret_key_or_no_text_is_said_in_place_and_not_sent():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="geheim")
    following = world.following(account)
    opened(world, following, entry)
    assert following.talk.send(entry.sid, "   ") == "Write a message to send it."
    assert following.talk.send(entry.sid, "nsec1" + "q" * 58)
    assert following.talk.outgoing(entry.sid) == []


def reactions_of(world, following, entry, target):
    detail = following.detail(entry.sid)
    bubble = next(m.bubble for m in detail.thread
                  if isinstance(m, threads.Message) and m.bubble.id == target)
    return shown_reactions(bubble.reactions, ME, following.talk.reaction_on(entry.sid, target))


def react(following, detail, entry, target, emoji):
    following.talk.react(entry.sid, target, EDITOR, emoji,
                         threads.own_reaction(detail.opened, target, ME))


def test_a_reaction_is_added_changed_and_taken_back():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="reagieren")
    world.message(prepared, "Perfekt.")
    following = world.following(account)
    detail = opened(world, following, entry)
    target = next(m.bubble.id for m in detail.thread if isinstance(m, threads.Message))
    heart, thumb = conversation.REACTIONS[0], conversation.REACTIONS[1]
    seen_before = following.rows()[0].view.last_message_seq
    account.signer.held = []
    react(following, detail, entry, target, heart)
    assert reactions_of(world, following, entry, target) == ({ME: heart}, True)   # dimmed
    account.signer.release()
    world.settle()
    assert reactions_of(world, following, entry, target) == ({ME: heart}, False)
    detail = following.detail(entry.sid)
    react(following, detail, entry, target, thumb)      # another one replaces it
    world.settle()
    assert reactions_of(world, following, entry, target) == ({ME: thumb}, False)
    detail = following.detail(entry.sid)
    react(following, detail, entry, target, thumb)      # the same again takes it back
    world.settle()
    assert reactions_of(world, following, entry, target) == ({}, False)
    hints = [json.loads(body)["kind_hint"] for verb, path, body, _h
             in world.server.requests if verb == "POST" and path.endswith("/messages")]
    assert hints[-3:] == ["reaction", "reaction", "retraction"]
    following.refresh()
    world.settle()
    row = following.rows()[0]
    assert row.view.last_message_seq == seen_before and not row.unread


def test_a_reaction_that_did_not_go_leaves_the_bubble_as_it_was_and_offers_again():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="reaktion-aus")
    world.message(prepared, "Passt.")
    following = world.following(account)
    detail = opened(world, following, entry)
    target = next(m.bubble.id for m in detail.thread if isinstance(m, threads.Message))
    for _ in range(4):
        world.server.inject("messages", DROP_BEFORE)
    react(following, detail, entry, target, conversation.REACTIONS[0])
    world.settle()
    while world.later.waiting:
        world.later.run()
        world.settle()
    out = following.talk.reaction_on(entry.sid, target)
    assert out.state is Sending.FAILED
    assert reactions_of(world, following, entry, target) == ({}, False)
    assert footer_words(out)[0].startswith("Reaction not sent.") and footer_words(out)[1] == "Again"
    following.talk.retry(out.key)
    world.settle()
    assert reactions_of(world, following, entry, target) == (
        {ME: conversation.REACTIONS[0]}, False)


def test_a_stored_reaction_is_not_dimmed_though_the_refresh_after_it_fails():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="gespeichert")
    world.message(prepared, "Fein.")
    following = world.following(account)
    detail = opened(world, following, entry)
    target = next(m.bubble.id for m in detail.thread if isinstance(m, threads.Message))
    react(following, detail, entry, target, conversation.REACTIONS[0])
    world.settle()
    for _ in range(8):
        world.server.inject("lookup", DROP_BEFORE)
    following.refresh()
    world.settle()
    assert reactions_of(world, following, entry, target) == (
        {ME: conversation.REACTIONS[0]}, False)


def test_withdrawing_moves_the_row_to_done_only_once_the_server_stored_it():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="zurueck")
    following = world.following(account)
    opened(world, following, entry)
    world.server.inject("withdraw", DROP_BEFORE)
    answers = []
    following.withdraw(entry.sid, answers.append)
    world.settle()
    assert answers[0].code == Code.OFFLINE
    assert following.rows()[0].situation is Situation.SUBMITTED
    following.withdraw(entry.sid, answers.append)
    world.settle()
    assert answers[1] is None and following.rows()[0].situation is Situation.WITHDRAWN
    assert following.rows()[0].section is Section.DONE


def test_a_version_is_opened_only_from_its_verified_envelope():
    world = FollowWorld()
    account = world.account()
    _prepared, entry = world.submit(account, d="fassung", content="Erster Absatz.")
    following = world.following(account)
    detail = opened(world, following, entry)
    assert detail.latest.work["content"] == "Erster Absatz." and detail.latest.rights
    # A server that serves another envelope under the same revision.
    record = world.record(entry.sid)
    record.current["envelope_sha256"] = "00" * 32
    again = world.following(world.account())
    detail = opened(world, again, entry)
    assert detail.latest is None and detail.not_confirmed


def test_a_detail_answered_with_another_submission_is_refused():
    # Lesson 3: a server that answers X with Y must not put Y's members,
    # timeline and version under X.
    world = FollowWorld()
    account = world.account()
    _x, asked = world.submit(account, d="gefragt")
    _y, other = world.submit(account, d="anderes")
    following = world.following(account)
    world.server.submissions[asked.sid] = world.server.submissions[other.sid]
    detail = opened(world, following, asked)
    assert not detail.complete and detail.members is None and detail.latest is None
    assert following.row(asked.sid).view.submission_id == asked.sid


def test_a_stopped_account_writes_no_row_no_list_and_no_read_state():
    world = FollowWorld()
    account = world.account()
    prepared, entry = world.submit(account, d="stopp", stored=False)
    world.message(prepared, "Noch da?")
    following = world.following(account, start=False)
    changes = []
    following.changed.connect(lambda: changes.append("rows"))
    following.detail_changed.connect(changes.append)
    following.start()                                   # the list read and the lookup go out
    following.stop()
    account.let_go()
    published = list(world.relay.published)
    world.settle()
    world.later.run()
    world.settle()
    assert changes == [] and world.news == []
    assert world.relay.published == published
    assert following.rows() == [] and following.detail(entry.sid) is None
    following.open(entry.sid)                           # late callers get nothing
    following.view_opened()
    following.refresh()
    assert following.talk.send(entry.sid, "zu spät") != ""
    world.settle()
    assert changes == [] and world.relay.published == published


def test_ready_for_release_rests_on_every_creators_signed_confirmation():
    # F5 M1 (contract 12.4, 11.3): the server says every creator confirmed
    # (ready for release) but holds no signed confirmation of the writer:
    # their app asks them to confirm. Once they did, Paul still confirms.
    world = FollowWorld()
    writer = world.account()
    prepared, entry = world.submit(writer, d="gemeinsam", title="Gemeinsam",
                                   participants=(OTHER,))
    sid = entry.sid
    world.record(sid).signer = "publication"
    world.decide(prepared, "accept", split=[{"pubkey": ME, "weight": 70, "role": "writer"},
                                            {"pubkey": OTHER, "weight": 30,
                                             "role": "contributor"}])
    record = world.record(sid)
    record.confirmations.update({ME: ["split"], OTHER: ["split", "rights"]})
    record.step = "ready_for_release"
    following = world.following(writer)
    following.open(sid)
    world.settle()
    assert following.row(sid).situation is Situation.CONFIRM
    world.confirm(writer, prepared)
    following.open(sid)
    world.settle()
    row = following.row(sid)
    assert row.situation is Situation.ACCEPTED and rules.waiting_for(row) == (OTHER,)
