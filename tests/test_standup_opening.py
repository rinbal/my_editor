# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify before opening anything (contract 12.4, feat/redaktion 0820f0d):
nothing is opened on the strength of a View, only from a revision's
verified signed body. The attack it stops: a server that mints its own
submission key, puts its pubkey in the View and serves wraps of it.
"""

from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QCoreApplication

from nostr import crypto, events, nip98
from nostr.standup import crypto as sc
from nostr.standup import formats, views
from nostr.standup import opening as op
from nostr.standup.former import Formers, parse_formers
from nostr.standup.opening import (
    MediaKeys,
    NotConfirmed,
    NotOpened,
    OpenedRevision,
    VerifiedRevision,
    open_envelope,
    open_submission_key,
    verify_revision,
)
from tests.standup_fakes import ADDRESS, KEY_EVENT, KeySigner, submission_for
from tests.standup_vectors import NOW, VECTORS, fixture_secret, pk, sk

SERVER = "https://standup.example"
CONTRIBUTOR = pk("contributor")
# Someone the submission is not for: no fixture key, a fresh one.
STRANGER_SK = crypto.generate_secret_key()
STRANGER = crypto.get_public_key(STRANGER_SK).hex()


def signed(body: dict, path: str, signer: bytes) -> dict:
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    unsigned = nip98.build_unsigned_auth_event(SERVER + path, "POST", text.encode("utf-8"),
                                               now=NOW)
    return {"event": events.sign_event(unsigned, signer), "body": text}


def view_of(body: dict, **changes) -> views.SubmissionView:
    data = {
        "submission_id": body["submission_id"], "publication": body["publication"],
        "kind": body["kind"], "title": body["title"], "writer": pk("writer"),
        "participants": list(body["participants"]), "state": "submitted", "step": None,
        "turn": {"type": "none", "pubkey": None, "pending": []}, "responsible": None,
        "marks": [], "signer": "writer", "section": None, "revision": body["revision"],
        "round": body["round"], "p_sub": body["p_sub"], "epoch": body["epoch"],
        "revision_hash": body["revision_hash"], "created_at": NOW, "updated_at": NOW,
        "last_seq": 1, "confirmations": {}, "published": None, "placed": False,
        "proposal_by": None, "later_round": False}
    data.update(changes)
    return views.parse_view(data)


def record_of(body: dict, action: dict, **changes) -> views.RevisionRecord:
    data = {"revision": body["revision"], "revision_hash": body["revision_hash"],
            "envelope": body["envelope"], "envelope_sha256": body["envelope_sha256"],
            "wraps": body["wraps"], "blobs": [], "created_at": NOW, "signed": action}
    data.update(changes)
    return views.parse_revision_record(data)


@pytest.fixture
def submitted():
    prepared = submission_for(sk("writer"), participants=(CONTRIBUTOR,))
    action = signed(prepared.body, "/api/submissions", sk("writer"))
    return prepared, action


def open_key(verified, recipient, secret, **kw):
    got = []
    open_submission_key(KeySigner(secret), verified, recipient, got.append, got.append, **kw)
    if got and isinstance(got[0], NotConfirmed):
        raise AssertionError("a refusal answered inside the call (RG-M2a 6)")
    QCoreApplication.processEvents()
    assert len(got) == 1
    return got[0]


# -- what was signed opens -----------------------------------------------------------

def test_a_contributor_opens_what_the_writer_signed(submitted):
    prepared, action = submitted
    body = prepared.body
    verified = verify_revision(view_of(body), record_of(body, action))
    assert (verified.p_sub, verified.revision_hash, verified.signer) == (
        body["p_sub"], body["revision_hash"], pk("writer"))
    s_sub = open_key(verified, CONTRIBUTOR, sk("contributor"),
                     served=body["wraps"][CONTRIBUTOR])
    assert s_sub == prepared.s_sub
    opened = open_envelope(verified, body["envelope"], s_sub)
    assert opened.revision == prepared.revision


# -- a server that mints its own key ---------------------------------------------------

def test_a_view_naming_a_key_the_writer_did_not_sign_opens_nothing(submitted):
    prepared, action = submitted
    body = prepared.body
    minted = sc.new_secret_key()
    forged = sc.wrap_submission_key(body["submission_id"], minted,
                                    {CONTRIBUTOR: CONTRIBUTOR})[CONTRIBUTOR]
    view = view_of(body, p_sub=sc.pubkey_of(minted))
    record = record_of(body, action, wraps=dict(body["wraps"], **{CONTRIBUTOR: forged}))
    with pytest.raises(NotConfirmed, match="submission key"):
        verify_revision(view, record)


def test_a_writer_the_server_picked_for_a_real_submission_id_opens_nothing():
    # RG-M1 2: the server keeps a real sid and serves a body of a writer
    # it controls, with its own key and envelope. The sid does not
    # recompute from that writer, so nothing opens.
    real = submission_for(sk("writer"), participants=(CONTRIBUTOR,))
    sid = real.body["submission_id"]
    puppet_sk = crypto.generate_secret_key()
    puppet = crypto.get_public_key(puppet_sk).hex()
    minted = sc.new_secret_key()
    forged = submission_for(puppet_sk, participants=(CONTRIBUTOR,), s_sub=minted,
                            content="Vom Server erfunden")
    body = dict(forged.body, submission_id=sid)
    sealed = sc.seal_revision(dict(forged.revision, submission_id=sid), minted)
    body.update(revision_hash=sealed.revision_hash, envelope=sealed.envelope_b64,
                envelope_sha256=sealed.envelope_sha256,
                wraps=sc.wrap_submission_key(sid, minted, {"redaktion": pk("writer"),
                                                           "writer": puppet,
                                                           CONTRIBUTOR: CONTRIBUTOR}))
    action = signed(body, "/api/submissions", puppet_sk)
    with pytest.raises(NotConfirmed, match="does not belong"):
        verify_revision(view_of(body, writer=puppet), record_of(body, action))


@pytest.mark.parametrize("field, value", [
    ("publication", f"{formats.SITE_KIND}:" + "ab" * 32 + ":andere"), ("round", 2)])
def test_a_body_for_another_publication_or_round_is_not_confirmed(submitted, field, value):
    prepared, action = submitted
    body = prepared.body
    with pytest.raises(NotConfirmed, match=f"the {field} signed is another"):
        verify_revision(view_of(body, **{field: value}), record_of(body, action))


def test_a_wrap_served_other_than_the_one_signed_is_not_opened(submitted):
    prepared, action = submitted
    body = prepared.body
    verified = verify_revision(view_of(body), record_of(body, action))
    forged = sc.wrap_submission_key(body["submission_id"], sc.new_secret_key(),
                                    {CONTRIBUTOR: CONTRIBUTOR})[CONTRIBUTOR]
    failure = open_key(verified, CONTRIBUTOR, sk("contributor"), served=forged)
    assert isinstance(failure, NotConfirmed)


def test_a_wrap_for_someone_else_is_not_there(submitted):
    prepared, action = submitted
    verified = verify_revision(view_of(prepared.body), record_of(prepared.body, action))
    assert isinstance(open_key(verified, STRANGER, STRANGER_SK), NotConfirmed)


@pytest.mark.parametrize("change", [
    lambda b, a: (view_of(b), record_of(b, a, revision=2)),
    lambda b, a: (view_of(b), record_of(b, a, revision_hash="ab" * 32)),
    lambda b, a: (view_of(b), record_of(b, a, envelope_sha256="ab" * 32)),
    lambda b, a: (view_of(b, kind="audio"), record_of(b, a)),
    lambda b, a: (view_of(b, title="Ein anderer Titel"), record_of(b, a)),
    lambda b, a: (view_of(b, revision_hash="ab" * 32), record_of(b, a)),
    lambda b, a: (view_of(b, writer=STRANGER), record_of(b, a)),
    lambda b, a: (view_of(b), record_of(b, signed(b, "/api/submissions", STRANGER_SK))),
    lambda b, a: (view_of(b), record_of(b, dict(a, body=a["body"].replace("Café", "Cafe")))),
    lambda b, a: (view_of(b), record_of(b, signed(b, "/api/submissions/x/withdraw",
                                                  sk("writer")))),
    lambda b, a: (view_of(b), record_of(b, None)),
])
def test_anything_shown_that_was_not_signed_is_not_confirmed(submitted, change):
    prepared, action = submitted
    view, record = change(prepared.body, action)
    with pytest.raises(NotConfirmed):
        verify_revision(view, record)


def test_a_revision_signed_as_true_is_not_revision_1(submitted):
    prepared, _action = submitted
    body = dict(prepared.body, revision=True)
    action = signed(body, "/api/submissions", sk("writer"))
    with pytest.raises(NotConfirmed, match="revision"):
        verify_revision(view_of(prepared.body), record_of(prepared.body, action))


@pytest.mark.parametrize("served", [{"eph": "x"}, {"eph": pk("writer"), "ct": 1.5},
                                    {"eph": pk("writer"), "ct": "eA==", "ü": 1}])
def test_a_served_wrap_of_the_wrong_shape_is_not_confirmed(submitted, served):
    prepared, action = submitted
    verified = verify_revision(view_of(prepared.body), record_of(prepared.body, action))
    assert isinstance(open_key(verified, CONTRIBUTOR, sk("contributor"), served=served),
                      NotConfirmed)


def test_an_envelope_other_than_the_one_signed_is_not_opened(submitted):
    prepared, action = submitted
    body = prepared.body
    verified = verify_revision(view_of(body), record_of(body, action))
    other = submission_for(sk("writer"), participants=(CONTRIBUTOR,), s_sub=prepared.s_sub,
                           content="# Anders\n\nEin anderer Text.")
    with pytest.raises(NotConfirmed):
        open_envelope(verified, other.body["envelope"], prepared.s_sub)
    with pytest.raises(NotConfirmed, match="submission"):
        open_envelope(verified, body["envelope"], sc.new_secret_key())


def test_a_signer_that_declines_has_not_opened_it_yet(submitted):
    prepared, action = submitted
    verified = verify_revision(view_of(prepared.body), record_of(prepared.body, action))
    got = []
    open_submission_key(KeySigner(sk("contributor"), refuse="user declined"), verified,
                        CONTRIBUTOR, got.append, got.append)
    assert isinstance(got[0], NotOpened) and not isinstance(got[0], NotConfirmed)


# -- corrections -----------------------------------------------------------------------

def correction_of(prepared, *, signer: bytes, epoch: int = 0):
    body = prepared.body
    sid = body["submission_id"]
    corrected = submission_for(sk("writer"), participants=(CONTRIBUTOR,), revision=2,
                               s_sub=prepared.s_sub, content="# Ein Jahr\n\nKorrigiert.")
    fix = {key: corrected.body[key] for key in ("submission_id", "revision", "revision_hash",
                                                 "envelope", "envelope_sha256", "wraps",
                                                 "blobs")}
    if epoch:
        fix["epoch"] = epoch
    action = signed(fix, f"/api/submissions/{sid}/corrections", signer)
    return corrected, record_of(corrected.body, action)


def test_a_correction_by_the_redaktion_takes_its_key_from_the_writers_revision(submitted):
    prepared, action = submitted
    body = prepared.body
    writer = verify_revision(view_of(body), record_of(body, action))
    corrected, record = correction_of(prepared, signer=sk("editor"))
    view = view_of(corrected.body)
    verified = verify_revision(view, record, redaktion={pk("editor")}, writer_revision=writer)
    assert verified.correction and verified.p_sub == body["p_sub"]
    assert open_envelope(verified, corrected.body["envelope"],
                         prepared.s_sub).revision == corrected.revision
    with pytest.raises(NotConfirmed):
        verify_revision(view, record, redaktion={pk("editor")})
    with pytest.raises(NotConfirmed):
        verify_revision(view, record, redaktion={STRANGER}, writer_revision=writer)


def test_a_correction_by_someone_who_left_verifies_when_they_decided_at_its_epoch(submitted):
    # Contract 10, People who left; vector former_member: `left` decides at
    # epoch 3, the curator invited again at epoch 3 does not.
    fm = VECTORS["former_member"]
    prepared, action = submitted
    writer = verify_revision(view_of(prepared.body), record_of(prepared.body, action))
    formers = Formers(fm["publication"])
    formers.join(parse_formers([fm["former"]["left"], fm["former"]["curator"]]))
    for label, person, verifies in (("former", "left", True), ("curator", "curator", False)):
        assert crypto.get_public_key(fixture_secret(label)).hex() == fm["former"][person]["pubkey"]
        corrected, record = correction_of(prepared, signer=fixture_secret(label), epoch=3)
        view = view_of(corrected.body)
        if verifies:
            verified = verify_revision(view, record, redaktion={pk("editor")},
                                       writer_revision=writer, formers=formers)
            assert verified.correction and verified.epoch == 3
        else:
            with pytest.raises(NotConfirmed):
                verify_revision(view, record, redaktion={pk("editor")},
                                writer_revision=writer, formers=formers)
        with pytest.raises(NotConfirmed):              # no word of the server's alone
            verify_revision(view, record, redaktion={pk("editor")}, writer_revision=writer)


# -- works already public (3b00351) ---------------------------------------------------

PUBLIC_D = "ein-jahr-mit-lightning"


def public_event(author_sk: bytes = None, *, d: str = PUBLIC_D, tags=()) -> dict:
    """A published kind 30023, signed by its author (the writer)."""
    unsigned = {"kind": formats.ARTICLE_KIND, "created_at": NOW, "content": "# Ein Jahr\n\nText.",
                "tags": [["d", d], ["title", "Ein Jahr mit Lightning im Café"], *tags]}
    return events.sign_event(unsigned, author_sk or sk("writer"))


def naddr_of(event: dict, d: str = PUBLIC_D) -> str:
    from nostr.bech32 import encode_naddr
    return encode_naddr(d, event["pubkey"], formats.ARTICLE_KIND)


def _public_parts(signer: str, event: dict, recipients: dict, round_: int = 1):
    coordinate = formats.coordinate_hash(event["pubkey"], PUBLIC_D)
    sid = formats.submission_id(signer, ADDRESS, coordinate, round_)
    s_sub = sc.new_secret_key()
    return sid, s_sub, coordinate, sc.wrap_submission_key(sid, s_sub, recipients)


def by_link(signer_sk: bytes = None, *, event: dict = None):
    """``POST /api/submissions`` with a ``source`` (D17): its revision is
    the event; built as core/editorial_submission.parse_submission reads it."""
    signer_sk = signer_sk or sk("writer")
    signer = crypto.get_public_key(signer_sk).hex()
    event = event or public_event()
    key = json.loads(KEY_EVENT["content"])
    sid, s_sub, coordinate, wraps = _public_parts(
        signer, event, {"redaktion": key["pubkey"], "writer": signer})
    body = {"v": 1, "submission_id": sid, "round": 1, "revision": 1, "revision_hash": event["id"],
            "publication": ADDRESS, "key_event_id": KEY_EVENT["id"], "epoch": key["epoch"],
            "kind": "article", "title": "Ein Jahr mit Lightning im Café",
            "coordinate_hash": coordinate, "signer": "writer", "participants": [],
            "p_sub": sc.pubkey_of(s_sub), "wraps": wraps, "status_key_hash": "ab" * 32,
            "client": "MyEditor tests", "source": {"naddr": naddr_of(event), "event": event}}
    return body, s_sub, signed(body, "/api/submissions", signer_sk)


def proposed(proposer_sk: bytes = None, *, event: dict = None, round_: int = 1,
             path: str = None):
    """``POST /api/sites/<route>/proposals`` by a member
    (core/editorial_submission.parse_proposal)."""
    proposer_sk = proposer_sk or sk("contributor")
    proposer = crypto.get_public_key(proposer_sk).hex()
    event = event or public_event()
    key = json.loads(KEY_EVENT["content"])
    sid, s_sub, _coordinate, wraps = _public_parts(
        proposer, event, {"redaktion": key["pubkey"], "proposer": proposer}, round_)
    body = {"naddr": naddr_of(event), "event": event, "round": round_, "submission_id": sid,
            "p_sub": sc.pubkey_of(s_sub), "wraps": wraps,
            "title": "Ein Jahr mit Lightning im Café"}
    action = signed(body, path or formats.site_path(ADDRESS) + "/proposals", proposer_sk)
    return body, s_sub, action


def public_view(body: dict, *, writer: str = None, proposal_by: str = None,
                **changes) -> views.SubmissionView:
    data = dict(submission_id=body["submission_id"], publication=ADDRESS, kind="article",
                title=body["title"], participants=[], revision=1, round=body["round"],
                p_sub=body["p_sub"], epoch=1, revision_hash=_revision_hash(body),
                marks=["already_public"] + (["proposal"] if proposal_by else []),
                writer=writer or pk("writer"), proposal_by=proposal_by)
    data.update(changes)
    return view_of({"submission_id": data["submission_id"], "publication": ADDRESS,
                    "kind": "article", "title": data["title"], "participants": [],
                    "revision": 1, "round": data["round"], "p_sub": data["p_sub"],
                    "epoch": 1, "revision_hash": data["revision_hash"]}, **data)


def _revision_hash(body: dict) -> str:
    """A link body names its revision; a proposal's is its event's id."""
    return body.get("revision_hash") or body["event"]["id"]


def public_record(body: dict, action: dict, **changes) -> views.RevisionRecord:
    data = {"revision": 1, "revision_hash": _revision_hash(body), "envelope": None,
            "envelope_sha256": None, "wraps": body["wraps"], "blobs": [], "created_at": NOW,
            "signed": action}
    data.update(changes)
    return views.parse_revision_record(data)


def test_a_work_handed_in_by_link_is_verified_by_its_event_and_its_key_opens():
    body, s_sub, action = by_link()
    verified = verify_revision(public_view(body), public_record(body, action))
    assert (verified.envelope_sha256, verified.revision_hash, verified.signer) == (
        None, body["source"]["event"]["id"], pk("writer"))
    assert open_key(verified, "writer", sk("writer"), served=body["wraps"]["writer"]) == s_sub
    with pytest.raises(NotConfirmed, match="no envelope"):
        open_envelope(verified, None, s_sub)        # nothing to open: it is public


def test_a_proposal_on_the_publication_is_verified_by_its_proposer_and_its_key_opens():
    body, s_sub, action = proposed()
    view = public_view(body, proposal_by=pk("contributor"))
    verified = verify_revision(view, public_record(body, action))
    assert (verified.signer, verified.kind, verified.revision) == (pk("contributor"), "article", 1)
    assert open_key(verified, "proposer", sk("contributor"),
                    served=body["wraps"]["proposer"]) == s_sub


def test_a_link_handed_in_by_someone_not_its_author_is_a_proposal_and_verified_as_one():
    body, s_sub, action = by_link(sk("contributor"))
    view = public_view(body, writer=pk("writer"), proposal_by=pk("contributor"))
    assert verify_revision(view, public_record(body, action)).signer == pk("contributor")
    # The same body shown as the contributor's own work hides the proposal.
    with pytest.raises(NotConfirmed, match="not its author"):
        verify_revision(public_view(body, writer=pk("contributor")), public_record(body, action))
    with pytest.raises(NotConfirmed, match="another writer"):
        verify_revision(public_view(body, writer=STRANGER, proposal_by=pk("contributor")),
                        public_record(body, action))
    # And the author's own link shown as a proposal of theirs is not one.
    own, _s_sub, own_action = by_link()
    with pytest.raises(NotConfirmed, match="another writer"):
        verify_revision(public_view(own, proposal_by=pk("writer")),
                        public_record(own, own_action))


def test_a_co_author_hands_a_work_in_by_link_as_its_writer():
    event = public_event(tags=[["p", pk("contributor"), "", "author"]])
    body, _s_sub, action = by_link(sk("contributor"), event=event)
    view = public_view(body, writer=pk("contributor"))
    assert verify_revision(view, public_record(body, action)).signer == pk("contributor")


def _forged_event(event: dict) -> dict:
    return dict(event, content=event["content"] + " Und mehr.")


@pytest.mark.parametrize("change, reason", [
    (lambda body: body["source"].update(event=_forged_event(body["source"]["event"])),
     "signature"),
    (lambda body: body["source"].update(naddr=naddr_of(body["source"]["event"], "anderes")),
     "another work"),
    (lambda body: body["source"].update(naddr="naddr1kaputt"), "malformed"),
    (lambda body: body.update(
        coordinate_hash=formats.coordinate_hash(pk("writer"), "anderes"),
        submission_id=formats.submission_id(pk("writer"), body["publication"],
                                            formats.coordinate_hash(pk("writer"), "anderes"), 1)),
     "address"),
    (lambda body: body.update(source="nichts"), "missing"),
], ids=["event forged", "naddr of another work", "naddr malformed", "coordinate", "no source"])
def test_a_link_whose_event_is_not_the_work_signed_opens_nothing(change, reason):
    body, _s_sub, _action = by_link()
    change(body)
    action = signed(body, "/api/submissions", sk("writer"))     # the writer signed even that
    with pytest.raises(NotConfirmed, match=reason):
        verify_revision(public_view(body), public_record(body, action))


def test_a_link_whose_revision_is_not_its_event_or_that_has_an_envelope_opens_nothing():
    body, _s_sub, action = by_link()
    with pytest.raises(NotConfirmed, match="envelope"):
        verify_revision(public_view(body), public_record(body, action,
                                                         envelope_sha256="ab" * 32))
    other = "cd" * 32
    body["revision_hash"] = other
    action = signed(body, "/api/submissions", sk("writer"))
    with pytest.raises(NotConfirmed, match="not the public work"):
        verify_revision(public_view(body, revision_hash=other),
                        public_record(body, action, revision_hash=other))


def test_an_envelope_shown_as_a_proposal_or_without_its_hash_opens_nothing(submitted):
    prepared, action = submitted
    body = prepared.body
    with pytest.raises(NotConfirmed, match="signed by someone"):
        verify_revision(view_of(body, proposal_by=pk("contributor")), record_of(body, action))
    del body["envelope_sha256"]
    action = signed(body, "/api/submissions", sk("writer"))
    with pytest.raises(NotConfirmed, match="no envelope"):
        verify_revision(view_of(body), record_of(dict(body, envelope_sha256=None), action))


@pytest.mark.parametrize("make, view_changes, record_changes, reason", [
    (lambda: proposed(), {"proposal_by": None}, {}, "signed by someone"),
    (lambda: proposed(), {"proposal_by": STRANGER}, {}, "signed by someone"),
    (lambda: proposed(), {"writer": STRANGER}, {}, "another writer"),
    (lambda: proposed(), {"round": 2}, {}, "round"),
    (lambda: proposed(round_=2), {"round": 2, "submission_id": None}, {}, "proposer"),
    (lambda: proposed(), {"p_sub": STRANGER}, {}, "submission key"),
    (lambda: proposed(), {"title": "Ein anderer Titel"}, {}, "not the one signed"),
    (lambda: proposed(), {"kind": "audio"}, {}, "kind"),
    (lambda: proposed(), {}, {"revision": 2}, "not the public work"),
    (lambda: proposed(), {}, {"envelope_sha256": "ab" * 32}, "not the public work"),
    (lambda: proposed(path="/api/sites/by/" + "ab" * 32 + "/eA/proposals"), {}, {},
     "another action"),
], ids=["no proposer shown", "another proposer", "another writer", "another round",
        "sid of another round", "another key", "another title", "another kind",
        "revision 2", "an envelope", "another publication"])
def test_a_proposal_shown_other_than_signed_opens_nothing(make, view_changes, record_changes,
                                                          reason):
    body, _s_sub, action = make()
    changes = {"proposal_by": pk("contributor"), **view_changes}
    if changes.get("submission_id", "") is None:
        # The sid of round 1 shown for a body signed for round 2.
        changes["submission_id"] = proposed()[0]["submission_id"]
        body = dict(body, submission_id=changes["submission_id"])
        action = signed(body, formats.site_path(ADDRESS) + "/proposals", sk("contributor"))
    with pytest.raises(NotConfirmed, match=reason):
        verify_revision(public_view(body, **changes), public_record(body, action,
                                                                    **record_changes))


# -- media keys ------------------------------------------------------------------------

def test_media_keys_come_only_from_a_verified_revision_and_stay_with_it(submitted):
    prepared, _action = submitted
    key = bytes(range(32))
    plain = b"Ein Bild" * 100
    encryptor = sc.MediaEncryptor(key, chunk_size=formats.MEDIA_CHUNK_MIN)
    data = encryptor.update(plain) + encryptor.finish()
    entry = sc.media_entry(key=key, encryptor=encryptor, name="a.png", mime="image/png")
    with_media = submission_for(sk("writer"), participants=(CONTRIBUTOR,),
                                s_sub=prepared.s_sub)
    revision = dict(with_media.revision, media=[entry])
    sealed = sc.seal_revision(revision, prepared.s_sub)
    body = dict(with_media.body, revision_hash=sealed.revision_hash,
                envelope=sealed.envelope_b64, envelope_sha256=sealed.envelope_sha256)
    action = signed(body, "/api/submissions", sk("writer"))
    verified = verify_revision(view_of(body), record_of(body, action))
    keys = MediaKeys()
    keys.add(open_envelope(verified, body["envelope"], prepared.s_sub))
    sid = body["submission_id"]
    assert keys.decrypt(sid, data, entry["cipher_sha256"]) == plain
    # Another submission naming the same bytes has no key here.
    with pytest.raises(NotConfirmed):
        keys.decrypt("cd" * 32, data, entry["cipher_sha256"])
    with pytest.raises(NotConfirmed):
        keys.decrypt(sid, data + b"x", entry["cipher_sha256"])


def test_two_revisions_naming_the_same_file_keep_both_keys(submitted):
    prepared, _action = submitted
    key = bytes(range(32))
    encryptor = sc.MediaEncryptor(key, chunk_size=formats.MEDIA_CHUNK_MIN)
    data = encryptor.update(b"Ton") + encryptor.finish()
    right = sc.media_entry(key=key, encryptor=encryptor, name="a.opus", mime="audio/ogg")
    wrong = dict(right, key=formats.b64encode(bytes(32)))
    keys = MediaKeys()
    for revision, entry in ((1, wrong), (2, right)):
        made = submission_for(sk("writer"), participants=(CONTRIBUTOR,), revision=revision,
                              s_sub=prepared.s_sub)
        sealed = sc.seal_revision(dict(made.revision, media=[entry]), prepared.s_sub)
        body = dict(made.body, revision_hash=sealed.revision_hash,
                    envelope=sealed.envelope_b64, envelope_sha256=sealed.envelope_sha256)
        verified = verify_revision(view_of(body), record_of(
            body, signed(body, "/api/submissions", sk("writer"))))
        keys.add(open_envelope(verified, body["envelope"], prepared.s_sub))
    assert len(keys.keys(body["submission_id"], right["cipher_sha256"])) == 2
    assert keys.decrypt(body["submission_id"], data, right["cipher_sha256"]) == b"Ton"


# -- no way around the verification (RG-M1 12) ------------------------------------

def test_a_verified_revision_comes_only_from_the_verification(submitted):
    prepared, action = submitted
    body = prepared.body
    verified = verify_revision(view_of(body), record_of(body, action))
    with pytest.raises(TypeError):
        VerifiedRevision(submission_id=body["submission_id"], revision=1,
                         revision_hash=body["revision_hash"],
                         envelope_sha256=body["envelope_sha256"], p_sub=body["p_sub"],
                         kind="article", title="x", wraps={}, signer=pk("writer"),
                         correction=False)
    with pytest.raises(TypeError):
        OpenedRevision(verified=verified, revision=prepared.revision)
    with pytest.raises(TypeError):
        MediaKeys().add(type("Forged", (), {"verified": verified,
                                            "revision": prepared.revision})())


def test_a_copy_with_other_values_opens_nothing(submitted):
    # RG-M2a 5: dataclasses.replace() kept the token, so it minted a
    # VerifiedRevision around the verification.
    from dataclasses import replace
    prepared, action = submitted
    body = prepared.body
    verified = verify_revision(view_of(body), record_of(body, action))
    forged = replace(verified, p_sub="11" * 32, wraps={"writer": {"eph": "x", "ct": "y"}})
    with pytest.raises(TypeError):
        open_submission_key(KeySigner(sk("writer")), forged, "writer", print, print)
    with pytest.raises(TypeError):
        open_envelope(forged, body["envelope"], prepared.s_sub)
    with pytest.raises(TypeError):
        verify_revision(view_of(body), record_of(body, action), writer_revision=forged)
    opened = open_envelope(verified, body["envelope"], prepared.s_sub)
    with pytest.raises(TypeError):
        MediaKeys().add(replace(opened, revision={"media": [{"cipher_sha256": "a" * 64,
                                                             "key": "AAAA"}]}))
    MediaKeys().add(opened)                                 # what was opened here counts


# The vectors' writer wrap.
WRAP = VECTORS["key_wrap"]["wraps"]["writer"]


def test_a_wrap_opens_through_the_signer_too():
    signer = KeySigner(sk("writer"))
    got = []
    op._open_wrap_with_signer(signer, WRAP, got.append, got.append)
    assert got[0]["type"] == "submission-key"
    op._open_wrap_with_signer(signer, {"eph": "x", "ct": "y"}, got.append, got.append)
    assert len(got) == 1                        # a refusal comes on a later turn
    QCoreApplication.processEvents()
    assert got[1] == "not a key wrap"


def test_a_wrap_that_holds_something_else_is_told_apart_from_a_signer_saying_no():
    invalid, failed = [], []
    other = sc.make_wrap({"v": 2, "type": "submission-key"}, pk("writer"))
    op._open_wrap_with_signer(KeySigner(sk("writer")), other, failed.append, failed.append,
                             on_invalid=invalid.append)
    assert isinstance(invalid[0], sc.WrapInvalid) and failed == []
    op._open_wrap_with_signer(KeySigner(sk("writer"), refuse="declined"), WRAP,
                             failed.append, failed.append, on_invalid=invalid.append)
    assert failed == ["declined"] and len(invalid) == 1


def test_the_openers_are_not_offered_to_other_modules():
    for name in ("open_revision", "open_wrap_with_signer", "open_wrap_local",
                 "decrypt_media", "check_submission_key", "decrypt_envelope",
                 "MediaDecryptor"):
        assert name not in sc.__all__ and not hasattr(sc, name), name


def test_media_keys_are_forgotten_on_sign_out(submitted):
    prepared, _action = submitted
    key = bytes(range(32))
    encryptor = sc.MediaEncryptor(key, chunk_size=formats.MEDIA_CHUNK_MIN)
    data = encryptor.update(b"Bild") + encryptor.finish()
    entry = sc.media_entry(key=key, encryptor=encryptor, name="a.png", mime="image/png")
    sealed = sc.seal_revision(dict(prepared.revision, media=[entry]), prepared.s_sub)
    body = dict(prepared.body, revision_hash=sealed.revision_hash,
                envelope=sealed.envelope_b64, envelope_sha256=sealed.envelope_sha256)
    verified = verify_revision(view_of(body), record_of(
        body, signed(body, "/api/submissions", sk("writer"))))
    keys = MediaKeys()
    keys.add(open_envelope(verified, body["envelope"], prepared.s_sub))
    assert keys.decrypt(body["submission_id"], data, entry["cipher_sha256"]) == b"Bild"
    keys.clear()
    with pytest.raises(NotConfirmed):
        keys.decrypt(body["submission_id"], data, entry["cipher_sha256"])
