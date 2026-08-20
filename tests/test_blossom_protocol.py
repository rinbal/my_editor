# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire-level conformance for ``BlossomClient``.

What goes out (``X-SHA-256``, the BUD-12 cursor, the base64url token)
and what is believed coming back (the descriptor hash, the status code,
``X-Reason``, a BUD-08 ``nip94`` block).

Every test injects a fake transport, so nothing here opens a socket, and
the ones that need a counterparty use ``FakeBlossomServer``, which
applies BUD-11's validation rules to us rather than assuming our own
reader is the standard.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl, QUrlQuery
from PySide6.QtWidgets import QApplication

from nostr.blossom import auth
from nostr.blossom.client import BlossomClient, UploadResult, parse_nip94
from nostr.blossom.errors import ERROR_CODES
from tests.blossom_fakes import (
    BODY,
    OTHER_SHA,
    PUBKEY,
    SERVER,
    SHA,
    FakeBlossomServer,
    FakeNam,
    auth_event,
    descriptor,
    error_reply,
    json_reply,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _client(replies=None, *, responder=None):
    nam = FakeNam(replies, responder=responder)
    return BlossomClient(nam=nam), nam


def _signed_upload_token(sha: str = SHA, server: str = SERVER) -> dict:
    """What the store actually signs, minus the signature."""
    event = auth.build_blossom_auth_event(
        "upload", file_hash=sha, server=server, pubkey_hex=PUBKEY
    )
    return {**event, "id": "ff" * 32, "sig": "aa" * 64}


def _raw(request, name: str) -> str:
    return bytes(request.rawHeader(name)).decode("ascii")


# --------------------------------------------------------------------------- #
# X-SHA-256 on upload
# --------------------------------------------------------------------------- #

def test_upload_sends_x_sha_256_matching_the_body():
    """BUD-11's endpoint table makes ``X-SHA-256`` the blob hash implied
    by ``PUT /upload``, so a server enforcing authorization cannot check
    the token's ``x`` tag without it."""
    client, nam = _client([json_reply(descriptor(), status=201)])
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  lambda *_a: None, lambda *_a: None)
    header = _raw(nam.calls[0][1], "X-SHA-256")
    assert header == SHA
    assert header == header.lower()
    assert header == hashlib.sha256(BODY).hexdigest()


def test_upload_hash_is_derived_from_the_body_not_the_caller():
    client, nam = _client([json_reply(descriptor(), status=201)])
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  lambda *_a: None, lambda *_a: None, sha256=SHA)
    assert _raw(nam.calls[0][1], "X-SHA-256") == SHA


def test_a_body_outside_the_tokens_x_tags_is_never_sent():
    """The pre-flight guard: a mis-wired caller must not put bytes on
    the wire under somebody else's token."""
    client, nam = _client()
    ok, err = [], []
    client.upload(SERVER, b"different bytes", "image/png",
                  _signed_upload_token(), ok.append, err.append)
    assert nam.calls == []
    assert ok == []
    assert err[0].code == ERROR_CODES.HASH_MISMATCH


def test_an_unscoped_token_still_uploads():
    client, nam = _client([json_reply(descriptor(), status=201)])
    event = auth_event(SERVER)
    event["tags"] = [["t", "upload"]]
    client.upload(SERVER, BODY, "image/png", event,
                  lambda *_a: None, lambda *_a: None)
    assert len(nam.calls) == 1


def test_mirror_never_sends_x_sha_256():
    """The mirror request body is JSON, not the blob, so hashing it
    would describe the wrong thing."""
    client, nam = _client([json_reply(descriptor())])
    client.mirror(SERVER, "https://src.example/i.png",
                  _signed_upload_token(), lambda *_a: None, lambda *_a: None,
                  sha256=SHA)
    request = nam.calls[0][1]
    assert not request.hasRawHeader("X-SHA-256")
    assert json.loads(nam.calls[0][2]) == {"url": "https://src.example/i.png"}


# --------------------------------------------------------------------------- #
# Response verification
# --------------------------------------------------------------------------- #

def test_a_descriptor_naming_another_blob_is_refused():
    """BUD-02: the server "MUST NOT modify the blob in any way and MUST
    compute the sha256 hash over the exact bytes received", so a
    different hash means the response is about a different file."""
    client, nam = _client([json_reply(descriptor(OTHER_SHA), status=201)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert ok == []
    assert err[0].code == ERROR_CODES.HASH_MISMATCH


def test_a_mirror_descriptor_disagreeing_with_the_x_tag_is_refused():
    client, nam = _client([json_reply(descriptor(OTHER_SHA))])
    ok, err = [], []
    client.mirror(SERVER, "https://src.example/i.png",
                  _signed_upload_token(), ok.append, err.append, sha256=SHA)
    nam.issued[0].finish()
    assert ok == []
    assert err[0].code == ERROR_CODES.HASH_MISMATCH


def test_from_json_without_an_expectation_still_parses():
    result = UploadResult.from_json(descriptor(OTHER_SHA), SERVER)
    assert result["hash"] == OTHER_SHA


def test_a_malformed_sha256_is_refused():
    with pytest.raises(Exception):
        UploadResult.from_json({"sha256": "nope"}, SERVER)


# --------------------------------------------------------------------------- #
# Status codes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("status, code", [
    (401, ERROR_CODES.AUTH_REJECTED),
    (403, ERROR_CODES.AUTH_REJECTED),
    (402, ERROR_CODES.PAYMENT_REQUIRED),
    (409, ERROR_CODES.HASH_MISMATCH),
    (413, ERROR_CODES.SERVER_TOO_LARGE),
    (429, ERROR_CODES.RATE_LIMITED),
])
def test_upload_status_codes_map_to_stable_codes(status, code):
    client, nam = _client([error_reply(status)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert ok == []
    assert err[0].code == code


def test_409_on_mirror_is_also_a_hash_mismatch():
    client, nam = _client([error_reply(409)])
    ok, err = [], []
    client.mirror(SERVER, "https://src.example/i.png",
                  _signed_upload_token(), ok.append, err.append, sha256=SHA)
    nam.issued[0].finish()
    assert err[0].code == ERROR_CODES.HASH_MISMATCH


def test_413_carries_the_servers_own_reason():
    client, nam = _client([error_reply(413, reason="Maximum is 10 MiB")])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert err[0].code == ERROR_CODES.SERVER_TOO_LARGE
    assert err[0].detail == "Maximum is 10 MiB"


def test_402_records_the_offered_methods_and_issues_no_retry():
    """BUD-07. The header payloads are a cashu token and a BOLT-11
    invoice; only the method names are kept, and nothing is retried."""
    client, nam = _client([
        error_reply(402, reason="Payment required",
                    headers={b"X-Lightning": b"lnbc30n1pnnmw3l..."}),
    ])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert len(nam.calls) == 1
    assert err[0].code == ERROR_CODES.PAYMENT_REQUIRED
    assert err[0].payment_methods == ("lightning",)
    assert "lnbc" not in repr(err[0].payment_methods)


def test_a_transport_failure_is_reported_as_unreachable():
    client, nam = _client([error_reply(0)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert err[0].code == ERROR_CODES.NETWORK_UNAVAILABLE


@pytest.mark.parametrize("status, existed", [(200, True), (201, False)])
def test_200_means_the_blob_already_existed(status, existed):
    """BUD-02: 201 when newly stored, 200 when it was already there.
    BUD-06 warns clients against requiring 201, so both succeed."""
    client, nam = _client([json_reply(descriptor(), status=status)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert err == []
    assert ok[0]["existed"] is existed


# --------------------------------------------------------------------------- #
# BUD-12 cursor pagination
# --------------------------------------------------------------------------- #

def _query(request):
    return QUrlQuery(QUrl(request.url()).query())


def test_first_page_carries_no_cursor():
    client, nam = _client([json_reply([])])
    client.list_for_pubkey(SERVER, PUBKEY, None, lambda *_a: None,
                           lambda *_a: None, limit=200)
    request = nam.calls[0][1]
    assert request.url().path() == f"/list/{PUBKEY}"
    query = _query(request)
    assert not query.hasQueryItem("cursor")
    assert query.queryItemValue("limit") == "200"


def test_a_cursor_and_limit_are_sent_as_query_items():
    client, nam = _client([json_reply([])])
    client.list_for_pubkey(SERVER, PUBKEY, None, lambda *_a: None,
                           lambda *_a: None, cursor=SHA, limit=200)
    query = _query(nam.calls[0][1])
    assert query.queryItemValue("cursor") == SHA
    assert query.queryItemValue("limit") == "200"
    assert nam.calls[0][1].url().path() == f"/list/{PUBKEY}"


def test_a_cursor_that_is_not_a_sha256_is_refused_before_sending():
    client, nam = _client()
    ok, err = [], []
    client.list_for_pubkey(SERVER, PUBKEY, None, ok.append, err.append,
                           cursor="../../etc/passwd")
    assert nam.calls == []
    assert err[0].code == ERROR_CODES.HASH_MISMATCH


def test_the_fake_server_paginates_the_way_bud12_says():
    """Guards the fixture the store tests rely on: newest first, and
    never including the blob at the cursor."""
    blobs = [descriptor(f"{i:064x}", uploaded=100 + i) for i in range(5)]
    server = FakeBlossomServer(SERVER, blobs=blobs, require_auth=False)
    client, nam = _client(responder=server)

    pages = []
    client.list_for_pubkey(SERVER, PUBKEY, None, pages.append,
                           lambda *_a: None, limit=2)
    nam.issued[0].finish()
    assert [b["sha256"] for b in pages[0]] == [f"{4:064x}", f"{3:064x}"]

    client.list_for_pubkey(SERVER, PUBKEY, None, pages.append,
                           lambda *_a: None, cursor=f"{3:064x}", limit=2)
    nam.issued[1].finish()
    assert [b["sha256"] for b in pages[1]] == [f"{2:064x}", f"{1:064x}"]


# --------------------------------------------------------------------------- #
# BUD-08 nip94 capture
# --------------------------------------------------------------------------- #

WELL_FORMED = [
    ["url", f"{SERVER}/{SHA}.pdf"],
    ["m", "application/pdf"],
    ["x", SHA],
    ["size", "184292"],
    ["magnet", "magnet:?xt=urn:btih:9804c5"],
    ["i", "9804c5286a3fb07b2244c968b39bc3cc814313bc"],
]


def test_well_formed_nip94_is_captured():
    result = UploadResult.from_json(descriptor(nip94=WELL_FORMED), SERVER)
    assert result["nip94"] == WELL_FORMED


def test_nip94_urls_must_clear_the_media_policy():
    raw = [
        ["url", "file:///etc/passwd"],
        ["fallback", "file:///etc/shadow"],
        ["thumb", "http://evil.example/t.png"],
        ["m", "image/png"],
        ["fallback", "https://mirror.example/x.png"],
    ]
    pairs = parse_nip94(raw, SHA)
    assert ["m", "image/png"] in pairs
    assert ["fallback", "https://mirror.example/x.png"] in pairs
    assert not any("file://" in value for _k, value in pairs)
    assert not any(key == "thumb" for key, _v in pairs)


def test_a_contradicting_nip94_x_is_dropped():
    """The server does not get to rename this app's blob."""
    pairs = parse_nip94([["x", OTHER_SHA], ["m", "image/png"]], SHA)
    assert pairs == [["m", "image/png"]]


def test_unknown_nip94_keys_are_dropped():
    pairs = parse_nip94([["evil", "1"], ["m", "image/png"]], SHA)
    assert pairs == [["m", "image/png"]]


def test_a_malformed_nip94_never_fails_a_good_upload():
    for raw in ("not a list", 5, {"m": "image/png"}, None,
                [["m"], ["m", 5], [5, "x"], "string", []]):
        result = UploadResult.from_json(descriptor(nip94=raw), SERVER)
        assert result["hash"] == SHA
        assert result["nip94"] == []


def test_nip94_is_capped_and_clipped():
    raw = [["alt", "y" * 5000]] + [["m", f"image/{i}"] for i in range(100)]
    pairs = parse_nip94(raw, SHA)
    assert len(pairs) == 32
    assert len(pairs[0][1]) == 512


def test_a_missing_nip94_yields_an_empty_list():
    assert UploadResult.from_json(descriptor(), SERVER)["nip94"] == []


# --------------------------------------------------------------------------- #
# A counterparty that validates
# --------------------------------------------------------------------------- #

def test_a_strict_server_accepts_what_the_app_actually_sends():
    """End to end against BUD-11's validation rules: base64url token,
    past ``created_at``, live ``expiration``, ``t=upload``, a bare
    domain ``server`` tag, an ``x`` tag, and ``X-SHA-256``."""
    server = FakeBlossomServer(SERVER)
    client, nam = _client(responder=server)
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", _signed_upload_token(),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert err == []
    assert ok[0]["hash"] == SHA
    assert server.uploads == [BODY]
    token = server.tokens[0]
    assert [t[1] for t in token["tags"] if t[0] == "server"] == ["good.example"]
    assert [t[1] for t in token["tags"] if t[0] == "x"] == [SHA]


def test_a_strict_server_refuses_a_full_origin_server_tag():
    """Not a regression test, a canary: it proves the strict fake really
    applies BUD-11 rule 5, so the acceptance above means something."""
    server = FakeBlossomServer(SERVER)
    client, nam = _client(responder=server)
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", auth_event(SERVER),
                  ok.append, err.append)
    nam.issued[0].finish()
    assert ok == []
    assert err[0].status == 401
