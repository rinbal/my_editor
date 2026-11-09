# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Verify before opening anything (contract 12.4, feat/redaktion 071f757):
nothing is opened on the strength of a View, only from a revision's
verified signed body. The attack it stops: a server that mints its own
submission key, puts its pubkey in the View and serves wraps of it.
"""

from __future__ import annotations

import json

import pytest

from nostr import crypto, events, nip98
from nostr.standup import crypto as sc
from nostr.standup import formats, views
from nostr.standup.opening import (
    MediaKeys,
    NotConfirmed,
    NotOpened,
    open_envelope,
    open_submission_key,
    verify_revision,
)
from tests.standup_fakes import KeySigner, submission_for
from tests.standup_vectors import NOW, pk, sk

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
        "proposal_by": None}
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

def correction_of(prepared, *, signer: bytes):
    body = prepared.body
    sid = body["submission_id"]
    corrected = submission_for(sk("writer"), participants=(CONTRIBUTOR,), revision=2,
                               s_sub=prepared.s_sub, content="# Ein Jahr\n\nKorrigiert.")
    fix = {key: corrected.body[key] for key in ("submission_id", "revision", "revision_hash",
                                                 "envelope", "envelope_sha256", "wraps",
                                                 "blobs")}
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

