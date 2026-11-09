# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Formats and encryption of STANDUP submissions (nostr/standup/formats.py,
nostr/standup/crypto.py) against the contract's shared vectors: canonical
JSON, the identifiers, the content key, the envelope, SMF1 media files and
key wraps, every vector that must fail included."""

from __future__ import annotations

import base64
import io
import json

import pytest

from nostr import crypto as nostr_crypto
from nostr import refs
from nostr.standup import crypto as sc
from nostr.standup import formats
from nostr.standup.formats import FormatError, SecretKeyFound
from tests.standup_vectors import ADDRESS, IDS, KEYS, SID, VECTORS, pk, sk

V = VECTORS


# -- canonical JSON (1) -----------------------------------------------------------

@pytest.mark.parametrize("case", V["canonical_json"], ids=lambda c: c["canonical"][:24])
def test_canonical_json_vectors(case):
    assert formats.canonical_json(case["input"]) == case["canonical"]


@pytest.mark.parametrize("value", [
    {"x": 1.5}, {"x": float("nan")}, {"x": -0.0}, {"x": 2 ** 53}, {"x": -(2 ** 53)},
    {"ä": 1}, {1: "a"}, {"x": object()}, {"x": "\ud800"},
])
def test_what_canonical_json_cannot_hold_is_refused(value):
    with pytest.raises(FormatError):
        formats.canonical_json(value)


def test_canonical_json_keeps_line_separators_and_delete_raw():
    assert formats.canonical_json({"t": "  \x7f\x1f"}) == '{"t":"  \x7f\\u001f"}'


def test_deep_nesting_is_refused_not_a_crash():
    deep = []
    for _ in range(100_000):
        deep = [deep]
    with pytest.raises(FormatError):
        formats.canonical_json(deep)


# -- identifiers (2, 13) -------------------------------------------------------------

def test_identifier_vectors():
    assert formats.coordinate_hash(IDS["writer"], IDS["d"]) == IDS["coordinate_hash"]
    assert formats.submission_id(IDS["writer"], IDS["publication"], IDS["coordinate_hash"],
                                 IDS["round"]) == IDS["submission_id"]
    assert formats.status_key_hash(IDS["status_key"]) == IDS["status_key_hash"]
    assert formats.read_state_d(IDS["writer"]) == IDS["read_state_d"]
    assert f"{formats.read_state_d(IDS['writer'])}:2" == IDS["read_state_d_page_2"]


def test_another_round_is_another_submission():
    first = formats.submission_id(IDS["writer"], ADDRESS, IDS["coordinate_hash"], 1)
    second = formats.submission_id(IDS["writer"], ADDRESS, IDS["coordinate_hash"], 2)
    assert first == SID and second != SID
    with pytest.raises(FormatError):
        formats.submission_id(IDS["writer"], ADDRESS, IDS["coordinate_hash"], 0)


def test_publication_address_and_route_key():
    pubkey, site_d = formats.parse_address(ADDRESS)
    assert pubkey == pk("publication") and site_d == "einundzwanzig-magazin"
    assert formats.route_key(pubkey, site_d) == f"by/{pubkey}/ZWludW5kendhbnppZy1tYWdhemlu"
    # Identifiers may hold slashes: URL-safe base64 keeps them out of the path.
    assert "/" not in formats.route_key(pubkey, "a/b?c").split("/", 2)[2]
    for bad in ("30023:" + pubkey + ":x", "30004:" + "0" * 64 + ":x", "30004:" + pubkey + ":",
                "30004:" + pubkey, None):
        with pytest.raises(FormatError):
            formats.parse_address(bad)


def test_the_key_event_names_its_site():
    assert formats.key_event_d("einundzwanzig-magazin") == (
        "standup:redaktion-key:einundzwanzig-magazin")
    assert formats.settings_event_d("x") == "standup:redaktion-settings:x"


def test_pubkeys_must_be_on_the_curve():
    assert formats.is_pubkey(pk("writer"))
    assert not formats.is_pubkey("0" * 64)
    assert not formats.is_pubkey(pk("writer").upper())


# -- content key (3) ------------------------------------------------------------------

def test_content_key_vector():
    ck = V["content_key"]
    assert ck["salt"] == "standup-editorial-v1" and ck["info"] == "content"
    assert sc.content_key(bytes.fromhex(ck["s_sub"])).hex() == ck["k"]
    assert sc.pubkey_of(bytes.fromhex(ck["s_sub"])) == ck["p_sub"]


# -- envelope (6) and revision (5) ------------------------------------------------------

def test_envelope_vector_decrypts_and_hashes():
    env = V["envelope"]
    data = base64.b64decode(env["envelope"])
    assert formats.sha256_hex(data) == env["envelope_sha256"]
    assert formats.envelope_aad(SID, env["revision"]["revision"]).decode() == env["aad"]
    plaintext = sc.decrypt_envelope(bytes.fromhex(env["k"]), data, SID, 1)
    assert plaintext.decode("utf-8") == env["plaintext"]
    assert formats.sha256_hex(plaintext) == env["revision_hash"]
    assert formats.revision_hash(env["revision"]) == env["revision_hash"]
    assert formats.canonical_json(env["revision"]) == env["plaintext"]


def test_envelope_encrypts_to_the_vector_with_its_nonce():
    env = V["envelope"]
    sealed = sc.encrypt_envelope(bytes.fromhex(env["k"]), env["plaintext"].encode(), SID, 1,
                                 nonce=bytes.fromhex(env["nonce"]))
    assert base64.b64encode(sealed).decode() == env["envelope"]


def test_envelope_with_the_wrong_aad_fails():
    env = V["envelope"]
    data = base64.b64decode(env["envelope"])
    assert env["must_fail"]["wrong_aad"].endswith(":2")
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_envelope(bytes.fromhex(env["k"]), data, SID, 2)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_envelope(bytes.fromhex(env["k"]), data, "0" * 64, 1)
    flipped = bytearray(data)
    flipped[20] ^= 1
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_envelope(bytes.fromhex(env["k"]), bytes(flipped), SID, 1)


def test_open_revision_checks_every_name():
    env = V["envelope"]
    s_sub = bytes.fromhex(V["content_key"]["s_sub"])
    opened = sc.open_revision(env["envelope"], s_sub, submission_id=SID, revision=1,
                              revision_hash=env["revision_hash"],
                              envelope_sha256=env["envelope_sha256"])
    assert opened == env["revision"]
    with pytest.raises(sc.StandupCryptoError):
        sc.open_revision(env["envelope"], s_sub, submission_id=SID, revision=1,
                         revision_hash="0" * 64)
    with pytest.raises(sc.StandupCryptoError):
        sc.open_revision(env["envelope"], s_sub, submission_id=SID, revision=1,
                         envelope_sha256="0" * 64)


def test_a_revision_built_here_is_the_vectors_revision():
    rev = V["envelope"]["revision"]
    built = formats.build_revision(
        submission_id=rev["submission_id"], revision=1, kind="article", work=rev["work"],
        credits=rev["credits"], rights=rev["rights"], checklist=rev["checklist"],
        media=rev["media"], client="fixture")
    assert built == rev


def test_seal_revision_round_trips():
    rev = V["envelope"]["revision"]
    s_sub = sc.new_secret_key()
    sealed = sc.seal_revision(rev, s_sub)
    assert sealed.revision_hash == V["envelope"]["revision_hash"]
    assert sealed.envelope_sha256 == formats.sha256_hex(sealed.envelope)
    assert sc.open_revision(sealed.envelope_b64, s_sub, submission_id=SID, revision=1,
                            revision_hash=sealed.revision_hash) == rev


@pytest.mark.parametrize("change", [
    lambda r: r.update(kind="video"),
    lambda r: r.update(type="other"),
    lambda r: r.update(v=2),
    lambda r: r["work"].update(title="x" * 301),
    lambda r: r.update(audio={"medium": "podcast"}),
    lambda r: r["checklist"][0].update(level="optional"),
    lambda r: r["media"][0].update(ref="standup-media:" + "0" * 64),
    lambda r: r["media"][0].update(chunk_size=1024),
    lambda r: r.update(thumb={"mime": "image/png", "data": ""}),
    lambda r: r.update(thumb={"mime": "image/webp", "data": base64.b64encode(b"x" * 30_000).decode()}),
    lambda r: r["credits"].append({"pubkey": "0" * 64, "part": "x"}),
])
def test_a_revision_off_the_contract_is_refused(change):
    rev = json.loads(json.dumps(V["envelope"]["revision"]))
    change(rev)
    with pytest.raises(FormatError):
        formats.check_revision(rev)


def test_an_audio_revision_needs_its_audio():
    rev = json.loads(json.dumps(V["envelope"]["revision"]))
    rev["kind"] = "audio"
    with pytest.raises(FormatError):
        formats.check_revision(rev)
    rev["audio"] = {"medium": "podcast", "media": rev["work"]["image"], "duration": 61,
                    "peaks": list(range(256))}
    assert formats.check_revision(rev) is rev
    rev["audio"]["peaks"] = [0] * 513
    with pytest.raises(FormatError):
        formats.check_revision(rev)


def test_unknown_revision_fields_are_ignored_by_readers():
    rev = json.loads(json.dumps(V["envelope"]["revision"]))
    rev["future"] = {"anything": True}
    assert formats.read_revision(formats.canonical_bytes(rev), submission_id_=SID,
                                 revision_number=1)["future"] == {"anything": True}
    with pytest.raises(FormatError):
        formats.read_revision(formats.canonical_bytes(rev), submission_id_="0" * 64,
                              revision_number=1)
    with pytest.raises(FormatError):
        formats.read_revision(formats.canonical_bytes(rev), submission_id_=SID,
                              revision_number=2)


def test_an_envelope_larger_than_four_mib_is_refused():
    with pytest.raises(sc.StandupCryptoError):
        sc.encrypt_envelope(bytes(32), b"x" * formats.MAX_ENVELOPE_BYTES, SID, 1)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_envelope(bytes(32), b"x" * (formats.MAX_ENVELOPE_BYTES + 1), SID, 1)


# -- secret keys never reach a publication (5) ---------------------------------------------

NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"


@pytest.mark.parametrize("field, text", [
    ("content", f"Mein Schlüssel {NSEC}"),
    ("summary", f"backup_{NSEC}"),
    ("content", "private key: " + "ab" * 32),
    ("content", "ab" * 32 + " ist mein nsec in hex"),
    ("title", "Privat " + "cd" * 32),
])
def test_a_revision_with_a_secret_key_is_refused_before_encrypting(field, text):
    rev = json.loads(json.dumps(V["envelope"]["revision"]))
    rev["work"][field] = text
    with pytest.raises(SecretKeyFound) as caught:
        sc.seal_revision(rev, sc.new_secret_key())
    assert NSEC not in str(caught.value) and "ab" * 32 not in str(caught.value)
    with pytest.raises(SecretKeyFound):
        formats.build_revision(submission_id=SID, revision=1, kind="article", work=rev["work"],
                               client="t")


def test_hex_that_is_not_called_a_key_passes():
    # A public key or an event id is 64 hex digits too.
    assert formats.secret_keys_in_texts([f"Autor {pk('writer')}, Event {'ab' * 32}"]) == []
    assert refs.hex_secret_keys_in("private\n" + "ab" * 32) == []
    assert refs.hex_secret_keys_in("private " + "ab" * 33) == []


def test_the_refusal_is_translated():
    import i18n
    i18n.install("de")
    try:
        text = i18n._(formats.SECRET_KEY_IN_SUBMISSION)
    finally:
        i18n.install("en")
    assert text != formats.SECRET_KEY_IN_SUBMISSION and "Schlüssel" in text


# -- media, SMF1 (7) -------------------------------------------------------------------

M = V["media"]
MEDIA_KEY = base64.b64decode(M["key"])


def test_media_vector_decrypts():
    data = bytes.fromhex(M["file_hex"])
    assert formats.sha256_hex(data) == M["cipher_sha256"]
    plain = sc.decrypt_media(data, MEDIA_KEY, cipher_sha256=M["cipher_sha256"],
                             plain_sha256=M["plain_sha256"])
    assert plain.hex() == M["plaintext_hex"]


def test_media_encrypts_to_the_vector_with_its_nonces():
    plain = bytes.fromhex(M["plaintext_hex"])
    file = sc.encrypt_media(plain, MEDIA_KEY, chunk_size=M["chunk_size"],
                            nonces=[bytes.fromhex(n) for n in M["nonces"]])
    assert file.hex() == M["file_hex"]
    assert len(file) == sc.media_cipher_size(len(plain), M["chunk_size"]) == 9092


def test_an_empty_media_file_is_one_empty_last_record():
    assert sc.decrypt_media(bytes.fromhex(M["empty_file_hex"]), MEDIA_KEY) == b""
    made = sc.encrypt_media(b"", MEDIA_KEY, chunk_size=4096)
    assert len(made) == 8 + 12 + 16 == sc.media_cipher_size(0, 4096)
    assert sc.decrypt_media(made, MEDIA_KEY) == b""


@pytest.mark.parametrize("name", sorted(M["must_fail"]))
def test_media_vectors_that_must_fail(name):
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_media(bytes.fromhex(M["must_fail"][name]), MEDIA_KEY)


def test_media_cut_at_any_record_boundary_or_extended_fails():
    plain = bytes(range(256)) * 64        # 16 KiB: four full chunks of 4 KiB
    file = sc.encrypt_media(plain, MEDIA_KEY, chunk_size=4096)
    record = 12 + 4096 + 16
    for records in range(1, 4):
        with pytest.raises(sc.StandupCryptoError):
            sc.decrypt_media(file[:8 + records * record], MEDIA_KEY)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_media(file + file[8:8 + record], MEDIA_KEY)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_media(file + b"\x00", MEDIA_KEY)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_media(b"SMF2" + file[4:], MEDIA_KEY)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_media(b"SMF1" + (1024).to_bytes(4, "big") + file[8:], MEDIA_KEY)
    with pytest.raises(sc.StandupCryptoError):
        sc.decrypt_media(file, bytes(32))
    assert sc.decrypt_media(file, MEDIA_KEY) == plain


def test_media_streams_in_pieces_of_any_size():
    plain = bytes(range(251)) * 1000      # not a multiple of the chunk
    encryptor = sc.MediaEncryptor(MEDIA_KEY, chunk_size=8192)
    out = io.BytesIO()
    for start in range(0, len(plain), 3001):
        out.write(encryptor.update(plain[start:start + 3001]))
    out.write(encryptor.finish())
    file = out.getvalue()
    assert encryptor.cipher_sha256 == formats.sha256_hex(file)
    assert encryptor.plain_sha256 == formats.sha256_hex(plain)
    assert encryptor.cipher_size == len(file) == sc.media_cipher_size(len(plain), 8192)
    decryptor = sc.MediaDecryptor(MEDIA_KEY)
    back = io.BytesIO()
    for start in range(0, len(file), 777):
        back.write(decryptor.update(file[start:start + 777]))
    back.write(decryptor.finish())
    assert back.getvalue() == plain
    assert decryptor.cipher_sha256 == encryptor.cipher_sha256
    entry = sc.media_entry(key=MEDIA_KEY, encryptor=encryptor, name="a.bin",
                           mime="application/octet-stream", servers=["https://m.example"])
    assert formats.check_media_entry(entry) is entry
    assert entry["chunk_size"] == 8192 and entry["size"] == len(plain)


def test_new_files_use_one_mib_chunks():
    assert formats.MEDIA_CHUNK_SIZE == 1_048_576
    file = sc.encrypt_media(b"x" * 10, MEDIA_KEY)
    assert int.from_bytes(file[4:8], "big") == 1_048_576


# -- key wraps (4) -------------------------------------------------------------------------

W = V["key_wrap"]


@pytest.mark.parametrize("name", sorted(W["wraps"]))
def test_wrap_vectors_open_for_each_recipient(name):
    wrap = W["wraps"][name]
    plain = sc.open_wrap_local(wrap, bytes.fromhex(W["recipients"][name]))
    assert formats.canonical_json(plain) == W["plaintext_canonical"]
    assert formats.wrap_sha256(wrap) == W["wraps_sha256"][name]
    assert sc.check_submission_key(plain, submission_id=SID, p_sub=pk("submission")) == sk(
        "submission")


def test_a_wrap_for_another_submission_must_fail():
    plain = sc.open_wrap_local(W["must_fail"]["wrong_binding"], sk("writer"))
    with pytest.raises(sc.WrapInvalid):
        sc.check_submission_key(plain, submission_id=SID, p_sub=pk("submission"))


def test_a_wrap_whose_key_is_not_the_submissions_must_fail():
    plain = sc.open_wrap_local(W["wraps"]["writer"], sk("writer"))
    with pytest.raises(sc.WrapInvalid):
        sc.check_submission_key(plain, submission_id=SID, p_sub=pk("writer"))
    with pytest.raises(sc.WrapInvalid):
        sc.open_wrap_local(W["wraps"]["writer"], sk("contributor"))


def test_redaktion_key_and_history_chain_vectors():
    key_event = json.loads(V["key_event"]["valid"]["content"])
    older = json.loads(V["key_event"]["older_epoch"]["content"])
    plain = sc.open_wrap_local(W["epoch_wrap"], sk("editor"))
    # The owner's app writes the same plaintext when it wraps an epoch key.
    assert plain == sc.redaktion_key_plaintext(ADDRESS, 3, sk("epoch3"))
    assert sc.check_redaktion_key(plain, publication=ADDRESS, epoch=3,
                                  epoch_pubkey=key_event["pubkey"]) == sk("epoch3")
    with pytest.raises(sc.WrapInvalid):
        sc.check_redaktion_key(plain, publication=ADDRESS, epoch=2,
                               epoch_pubkey=key_event["pubkey"])
    with pytest.raises(sc.WrapInvalid):
        sc.check_redaktion_key(plain, publication=ADDRESS, epoch=3,
                               epoch_pubkey=older["pubkey"])
    chain = sc.open_wrap_local(W["history_wrap"], sk("epoch3"))
    assert sc.check_history_chain(chain, publication=ADDRESS, epoch=3,
                                  epoch_pubkeys={2: older["pubkey"]}) == {2: sk("epoch2")}
    with pytest.raises(sc.WrapInvalid):
        sc.check_history_chain(chain, publication=ADDRESS, epoch=3,
                               epoch_pubkeys={2: key_event["pubkey"]})
    with pytest.raises(sc.WrapInvalid):
        sc.check_submission_key(plain, submission_id=SID, p_sub=pk("epoch3"))


def test_wraps_made_here_open_for_each_recipient():
    recipients = {"redaktion": pk("epoch3"), "writer": pk("writer"),
                  pk("contributor"): pk("contributor")}
    wraps = sc.wrap_submission_key(SID, sk("submission"), recipients)
    assert set(wraps) == set(recipients)
    assert len({w["eph"] for w in wraps.values()}) == 3        # a fresh key per wrap
    for name, secret in (("redaktion", "epoch3"), ("writer", "writer"),
                         (pk("contributor"), "contributor")):
        plain = sc.open_wrap_local(wraps[name], sk(secret))
        assert plain == {"v": 1, "type": "submission-key", "submission_id": SID,
                         "sk": KEYS["submission"]["sk"]}
    assert sc.wraps_sha256(wraps)["writer"] == formats.wrap_sha256(wraps["writer"])


def test_a_wrap_opens_through_the_signer_too():
    from tests.standup_fakes import KeySigner
    signer = KeySigner(sk("writer"))
    got = []
    sc.open_wrap_with_signer(signer, W["wraps"]["writer"], got.append, got.append)
    assert got[0]["type"] == "submission-key"
    sc.open_wrap_with_signer(signer, {"eph": "x", "ct": "y"}, got.append, got.append)
    assert got[1] == "not a key wrap"


@pytest.mark.parametrize("wrap", [None, {}, {"eph": "0" * 64, "ct": "x"},
                                  {"eph": pk("writer"), "ct": "x", "extra": 1}])
def test_a_wrap_of_the_wrong_shape_is_not_opened(wrap):
    with pytest.raises(sc.WrapInvalid):
        sc.open_wrap_local(wrap, sk("writer"))


def test_status_and_submission_keys_are_fresh():
    assert formats.is_hex64(sc.new_status_key()) and sc.new_status_key() != sc.new_status_key()
    s = sc.new_secret_key()
    assert len(s) == 32 and nostr_crypto.get_public_key(s).hex() == sc.pubkey_of(s)
