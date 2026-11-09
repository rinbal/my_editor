# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A submission made of an article (nostr/standup/drafting.py): what
keeps the sheet's Einreichen dimmed, the round a text goes in, and the
body built for the verified key, with a wrap for every co-author."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nostr import crypto as nostr_crypto
from nostr.media.assets import asset_key
from nostr.standup import drafting, formats, submission, trust
from nostr.standup.drafting import ArticleDraft, Choice, LocalPicture
from nostr.standup.entries import SubmissionEntry
from tests.standup_fakes import ADDRESS, KEY_EVENT
from tests.standup_vectors import NOW, pk

DRAFT = ArticleDraft(d="ein-jahr", title="Ein Jahr mit Lightning", summary="Kurz.",
                     content="Text.")
CHOICE = Choice(publication=ADDRESS, name="Einundzwanzig Magazin", signer="writer",
                rights=True)


def _blockers(draft=DRAFT, **kw):
    kw.setdefault("rights", True)
    kw.setdefault("rights_required", True)
    return drafting.blockers(draft, writer=pk("writer"), **kw)


def test_an_article_with_its_title_and_rights_has_no_blocker():
    assert _blockers() == []


@pytest.mark.parametrize("draft, kw, words", [
    (replace(DRAFT, title="  "), {}, drafting.TITLE_MISSING),
    (replace(DRAFT, title="T" * 301), {}, drafting.TITLE_TOO_LONG.format(count=300)),
    (DRAFT, {"message": "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"},
     formats.SECRET_KEY_IN_SUBMISSION),
    (DRAFT, {"private_names": ["cafe.jpg"]}, drafting.PRIVATE_PICTURE.format(name="cafe.jpg")),
    (replace(DRAFT, pictures=tuple(LocalPicture(key=asset_key(f"{i:064x}"), name="p",
                                                mime="image/png") for i in range(65))),
     {}, drafting.TOO_MANY_PICTURES.format(count=64)),
    (replace(DRAFT, authors=tuple((nostr_crypto.get_public_key(bytes([i + 1]) * 32).hex(), "")
                                  for i in range(21))), {},
     drafting.TOO_MANY_PEOPLE.format(count=20)),
    (DRAFT, {"message": "x" * 16_001}, drafting.MESSAGE_TOO_LONG.format(count=16_000)),
    (DRAFT, {"submitted_to": "Einundzwanzig Magazin"},
     drafting.ALREADY_SUBMITTED.format(publication="Einundzwanzig Magazin")),
    (DRAFT, {"rights": False}, drafting.RIGHTS_MISSING),
])
def test_each_blocker_says_why(draft, kw, words):
    assert _blockers(draft, **kw) == [words]


@pytest.mark.parametrize("key", ["ff" * 32, "AB" * 32, "npub1xyz", ""])
def test_a_coauthor_whose_key_is_not_one_blocks_and_names_it_in_the_detail(key):
    # RG-M2b 11: an address imported from another app's tags reached the
    # seal and read as the server's refusal, with no way on.
    draft = replace(DRAFT, authors=((key, ""),))
    (found,) = _blockers(draft)
    assert found == drafting.COAUTHOR_INVALID and isinstance(found, drafting.Blocker)
    assert found.detail == drafting.COAUTHOR_INVALID_DETAIL.format(key=key)
    key_event = trust.verify_key_event(KEY_EVENT, ADDRESS)
    with pytest.raises(formats.FormatError):
        drafting.seal(draft, CHOICE, key_event, (), {}, writer=pk("writer"), round_=1, now=NOW)


@pytest.mark.parametrize("mime", ["image/svg+xml", "image/tiff", "image/heic", ""])
def test_a_picture_of_a_type_the_editorial_team_cannot_show_blocks_naming_the_file(mime):
    # Contract 5 (feat/redaktion ee3e5d7): an SVG can carry script.
    picture = LocalPicture(key=asset_key("a" * 64), name="logo.svg", mime=mime, data=b"x")
    (found,) = _blockers(replace(DRAFT, pictures=(picture,)))
    assert found == drafting.PICTURE_TYPE.format(name="logo.svg")
    assert found.detail == drafting.PICTURE_TYPE_DETAIL.format(type=mime or "?")
    key_event = trust.verify_key_event(KEY_EVENT, ADDRESS)
    with pytest.raises(formats.FormatError):
        drafting.seal(DRAFT, CHOICE, key_event, drafting.encrypt_pictures((picture,)), {},
                      writer=pk("writer"), round_=1, now=NOW)


@pytest.mark.parametrize("mime", formats.PICTURE_MIMES + ("IMAGE/PNG",))
def test_the_five_picture_types_are_taken(mime):
    picture = LocalPicture(key=asset_key("a" * 64), name="p", mime=mime, data=b"x")
    assert _blockers(replace(DRAFT, pictures=(picture,))) == []


CHECKLIST = [{"id": "title", "label": "Titel", "level": "required"},
             {"id": "summary", "label": "Zusammenfassung", "level": "required"},
             {"id": "image", "label": "Titelbild", "level": "required"},
             {"id": "tags", "label": "Schlagwörter", "level": "recommended"},
             {"id": "lang", "label": "Sprache", "level": "required"},
             {"id": "quellen", "label": "Quellen", "level": "required"},
             {"id": 3, "label": "kaputt", "level": "required"},
             {"id": "x", "label": "Ebene", "level": "sometimes"}]


def test_each_checklist_point_the_work_can_say_is_checked_the_others_are_unchecked():
    # RG-M2b 11: the revision carried no results. The ids are the ones
    # STANDUP's own writer checks; a broken point is left out.
    draft = replace(DRAFT, summary="", tags=("bitcoin",), lang="de")
    assert [(i["id"], i["result"]) for i in drafting.checklist_results(CHECKLIST, draft)] == [
        ("title", "ok"), ("summary", "missing"), ("image", "missing"), ("tags", "ok"),
        ("lang", "ok"), ("quellen", "unchecked")]
    assert drafting.checklist_results(None, draft) == []


def test_a_required_point_article_details_sets_blocks_and_one_it_cannot_does_not():
    draft = replace(DRAFT, summary="")
    (found,) = _blockers(draft, checklist=CHECKLIST, publication="Einundzwanzig Magazin")
    assert found == drafting.REQUIRED_MISSING.format(publication="Einundzwanzig Magazin",
                                                     items="Zusammenfassung, Titelbild")
    # The language has no place in Article Details: never a dead end.
    complete = replace(DRAFT, image="https://media.example/a.jpg")
    assert _blockers(complete, checklist=CHECKLIST, publication="M") == []


def test_the_revision_carries_the_checklist_results_and_the_articles_language():
    from nostr.standup import crypto as sc
    key = replace(trust.verify_key_event(KEY_EVENT, ADDRESS),
                  rules=dict(trust.verify_key_event(KEY_EVENT, ADDRESS).rules,
                             checklist=CHECKLIST[:5]))
    draft = replace(DRAFT, lang="de")
    sealed = drafting.seal(draft, CHOICE, key, (), {}, writer=pk("writer"), round_=1, now=NOW)
    revision = sc._open_revision(sealed.body["envelope"], sealed.s_sub,
                                 submission_id=sealed.body["submission_id"], revision=1)
    assert revision["work"]["lang"] == "de"
    assert [(i["id"], i["result"]) for i in revision["checklist"]] == [
        ("title", "ok"), ("summary", "ok"), ("image", "missing"), ("tags", "missing"),
        ("lang", "ok")]


def test_the_rights_block_only_where_the_publication_requires_them():
    assert _blockers(rights=False, rights_required=False) == []


def _entry(round_=1):
    return SubmissionEntry(sid="ab" * 32, publication=ADDRESS, server="https://s.example",
                           role="writer", s_sub="11" * 32, status_key="22" * 32, title="T",
                           kind="article", d="ein-jahr", round=round_, revision=1,
                           revision_hash="33" * 32, updated_at=NOW)


def _round_of(entries_states):
    entries = [replace(_entry(round_), sid=f"{round_:064x}") for round_, _state in entries_states]
    states = {entry.sid: state for entry, (_r, state) in zip(entries, entries_states)}
    choice = drafting.choose_round(entries, states)
    return choice.round, choice.unsent.round if choice.unsent else None, \
        choice.open.round if choice.open else None


@pytest.mark.parametrize("entries, expected", [
    ([], (1, None, None)),                                  # never submitted there
    ([(2, None)], (2, 2, None)),                            # no receipt: sent again
    ([(2, "declined")], (3, None, None)),
    ([(1, "declined"), (2, "withdrawn")], (3, None, None)),
    ([(2, "submitted")], (None, None, 2)),                  # still open there
    ([(1, "declined"), (2, "accepted")], (None, None, 2)),
    ([(1, None), (2, "declined")], (3, None, None)),        # before the last closed: never
    ([(1, "declined"), (3, None), (2, None)], (2, 2, None)),  # the next one first
], ids=["none", "unsent", "declined", "withdrawn", "open", "accepted", "stale unsent",
        "next unsent"])
def test_the_round_comes_from_what_the_server_reports(entries, expected):
    # Contract 2, "Choosing the round and the secrets".
    assert _round_of(entries) == expected


def test_a_round_is_never_taken_from_the_list_alone():
    with pytest.raises(KeyError):
        drafting.choose_round([_entry(1)], {})


def test_a_body_built_again_keeps_the_secrets_its_sid_holds_on_the_list():
    key = trust.verify_key_event(KEY_EVENT, ADDRESS)
    first = drafting.seal(DRAFT, CHOICE, key, (), {}, writer=pk("writer"), round_=1, now=NOW)
    again = drafting.seal(replace(DRAFT, title="Neu"), CHOICE, key, (), {}, writer=pk("writer"),
                          round_=1, now=NOW, held=first.entry)
    assert (again.entry.s_sub, again.entry.status_key) == (first.entry.s_sub,
                                                           first.entry.status_key)
    assert again.body["p_sub"] == first.body["p_sub"]
    fresh = drafting.seal(DRAFT, CHOICE, key, (), {}, writer=pk("writer"), round_=1, now=NOW)
    assert fresh.entry.s_sub != first.entry.s_sub
    for wrong in (replace(first.entry, sid="ab" * 32), replace(first.entry, status_key=None),
                  replace(first.entry, role="contributor")):
        with pytest.raises(drafting.HeldNotUsable):
            drafting.seal(DRAFT, CHOICE, key, (), {}, writer=pk("writer"), round_=1, now=NOW,
                          held=wrong)


def test_every_co_author_takes_part_with_a_wrap_of_their_own():
    key = trust.verify_key_event(KEY_EVENT, ADDRESS)
    draft = replace(DRAFT, authors=((pk("contributor"), "wss://relay.example"),
                                    (pk("writer"), ""), (pk("contributor"), "")))
    sealed = drafting.seal(draft, CHOICE, key, (), {}, writer=pk("writer"), round_=1, now=NOW)
    body = sealed.body
    assert body["participants"] == [pk("contributor")]
    assert set(body["wraps"]) == {"redaktion", "writer", pk("contributor")}
    assert submission.submission_body_problem(body) is None
    assert sealed.entry.sid == body["submission_id"] and sealed.entry.server == key.server
    assert sealed.entry.s_sub == sealed.s_sub.hex()


def test_a_revision_larger_than_the_publication_takes_is_refused_before_anything_is_sent():
    key = trust.verify_key_event(KEY_EVENT, ADDRESS)
    small = replace(key, rules=dict(key.rules, max_envelope_bytes=200))
    with pytest.raises(drafting.TooLarge):
        drafting.seal(DRAFT, CHOICE, small, (), {}, writer=pk("writer"), round_=1, now=NOW)
    drafting.seal(DRAFT, CHOICE, key, (), {}, writer=pk("writer"), round_=1, now=NOW)


def test_a_picture_is_named_on_at_most_ten_servers():
    picture, = drafting.encrypt_pictures([LocalPicture(key=asset_key("ab" * 32), name="a.png",
                                                       mime="image/png", data=b"png")])
    servers = [f"https://m{i}.example" for i in range(12)]
    assert len(picture.blob(servers)["servers"]) == 10
    assert len(picture.entry(servers)["servers"]) == 10
    assert picture.blob(servers)["size"] == len(picture.cipher)
