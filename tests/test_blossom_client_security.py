# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the untrusted-boundary behaviour of ``BlossomClient``.

The defect classes this file guards against:
- a redirect carrying the signed authorization event, and on PUT the
  whole file body, to whatever host ``Location`` names,
- an auth event signed for one server being sent to another,
- a server descriptor naming ``file://`` (verified: it read a local
  file into the cache) or another origin entering the app,
- an unbounded response body being buffered before any cap applies.

Every test injects a fake transport, so nothing here opens a socket.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QNetworkRequest
from PySide6.QtWidgets import QApplication

from nostr.blossom.client import (
    BlossomClient,
    UploadResult,
    server_origin,
)
from nostr.blossom.errors import ERROR_CODES
from tests.blossom_fakes import (
    BODY,
    PUBKEY,
    SERVER,
    SHA,
    FakeNam,
    FakeReply,
    auth_event,
    redirect_reply,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


DESCRIPTOR = {"sha256": SHA, "url": f"{SERVER}/{SHA}", "size": 3,
              "type": "image/png"}


def _client(replies=None):
    nam = FakeNam(replies)
    return BlossomClient(nam=nam), nam


def _ok_reply(payload) -> FakeReply:
    return FakeReply(status=200, body=json.dumps(payload).encode("utf-8"))


def _drive(verb, client, ok, err, *, server=SERVER):
    """Issue one request of each kind through a uniform interface."""
    if verb == "upload":
        client.upload(server, BODY, "image/png", auth_event(server), ok, err)
    elif verb == "mirror":
        client.mirror(server, "https://src.example/i.png",
                      auth_event(server), ok, err)
    elif verb == "list":
        client.list_for_pubkey(server, PUBKEY, auth_event(server, "list"),
                               ok, err)
    elif verb == "delete":
        client.delete(server, SHA, auth_event(server, "delete"), ok, err)
    else:  # pragma: no cover, guards a typo in the parametrisation
        raise AssertionError(verb)


VERBS = ["upload", "mirror", "list", "delete"]


# --------------------------------------------------------------------------- #
# Redirect policy
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("verb", VERBS)
def test_every_request_refuses_redirects_up_front(verb):
    client, nam = _client()
    _drive(verb, client, lambda *_a: None, lambda *_a: None)
    request = nam.calls[0][1]
    policy = request.attribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute)
    assert policy == QNetworkRequest.RedirectPolicy.ManualRedirectPolicy
    assert request.maximumRedirectsAllowed() == 0


@pytest.mark.parametrize("verb", VERBS)
def test_a_redirect_response_fails_with_a_stable_code(verb):
    client, nam = _client([redirect_reply()])
    ok, err = [], []
    _drive(verb, client, ok.append, err.append)
    nam.issued[0].finish()
    assert ok == []
    assert len(err) == 1
    assert err[0].code == ERROR_CODES.REDIRECT_REFUSED


# --------------------------------------------------------------------------- #
# Auth event scoping
# --------------------------------------------------------------------------- #

def test_auth_event_for_another_server_is_never_sent():
    client, nam = _client()
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png",
                  auth_event("https://evil.example"), ok.append, err.append)
    assert nam.calls == []
    assert ok == []
    assert err[0].code == ERROR_CODES.HOST_MISMATCH


def test_matching_host_issues_exactly_one_put():
    client, nam = _client([_ok_reply(DESCRIPTOR)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", auth_event(SERVER),
                  ok.append, err.append)
    assert [verb for verb, _r, _b in nam.calls] == ["put"]
    assert nam.calls[0][2] == BODY
    nam.issued[0].finish()
    assert err == []
    assert ok[0]["hash"] == SHA


def test_bare_domain_server_tag_is_accepted():
    # BUD-11 mandates a bare lowercase domain; the full origin is what
    # the app writes today. Both must reach the same host check.
    client, nam = _client([_ok_reply(DESCRIPTOR)])
    ok, err = [], []
    event = auth_event(SERVER)
    event["tags"] = [["t", "upload"], ["server", "good.example"]]
    client.upload(SERVER, BODY, "image/png", event, ok.append, err.append)
    assert len(nam.calls) == 1
    nam.issued[0].finish()
    assert err == []


def test_full_origin_server_tag_still_accepted():
    # Tokens minted before the BUD-11 fix carry a full origin. The
    # reader stays tolerant so a queued upload does not break.
    client, nam = _client([_ok_reply(DESCRIPTOR)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", auth_event(SERVER),
                  ok.append, err.append)
    assert len(nam.calls) == 1
    nam.issued[0].finish()
    assert err == []


def test_multi_server_token_matches_any_named_host():
    """BUD-11: "Multiple ``server`` tags may be present to allow the
    token to be used on multiple servers", and a server validates by
    finding its own domain in ANY of them. Reading only the first tag
    refused a perfectly valid token."""
    client, nam = _client([_ok_reply(DESCRIPTOR)])
    ok, err = [], []
    event = auth_event(SERVER)
    event["tags"] = [
        ["t", "upload"],
        ["server", "other.example"],
        ["server", "good.example"],
    ]
    client.upload(SERVER, BODY, "image/png", event, ok.append, err.append)
    assert len(nam.calls) == 1
    nam.issued[0].finish()
    assert err == []


def test_multi_server_token_still_refuses_an_unnamed_host():
    client, nam = _client()
    ok, err = [], []
    event = auth_event(SERVER)
    event["tags"] = [
        ["t", "upload"],
        ["server", "other.example"],
        ["server", "third.example"],
    ]
    client.upload(SERVER, BODY, "image/png", event, ok.append, err.append)
    assert nam.calls == []
    assert err[0].code == ERROR_CODES.HOST_MISMATCH


def test_missing_server_tag_skips_the_check():
    client, nam = _client([_ok_reply([])])
    event = auth_event(SERVER, "list")
    event["tags"] = [["t", "list"]]
    client.list_for_pubkey(SERVER, PUBKEY, event, lambda *_a: None,
                           lambda *_a: None)
    assert len(nam.calls) == 1


# --------------------------------------------------------------------------- #
# Descriptor URL validation
# --------------------------------------------------------------------------- #

def test_from_json_keeps_a_same_origin_https_url():
    result = UploadResult.from_json(dict(DESCRIPTOR), SERVER)
    assert result["url"] == f"{SERVER}/{SHA}"


@pytest.mark.parametrize("hostile", [
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "http://evil.example/x",
    f"https://other.example/{SHA}",
    "javascript:alert(1)",
])
def test_from_json_rewrites_an_unacceptable_url(hostile):
    payload = dict(DESCRIPTOR, url=hostile)
    result = UploadResult.from_json(payload, SERVER)
    assert result["url"] == f"{SERVER}/{SHA}"


def test_from_json_fills_in_a_missing_url():
    payload = {"sha256": SHA, "size": 3, "type": "image/png"}
    assert UploadResult.from_json(payload, SERVER)["url"] == f"{SERVER}/{SHA}"


# --------------------------------------------------------------------------- #
# The URL has to name the blob, not just come from the right host          #
# --------------------------------------------------------------------------- #

def test_a_same_origin_url_naming_another_blob_is_rewritten():
    # Right host, wrong blob. Every client applying the BUD-03 rule would
    # read this stored URL as the other hash, so it is replaced with the
    # canonical address rather than kept: BUD-01 serves every endpoint
    # from the root of the domain, so the canonical form always works.
    payload = dict(DESCRIPTOR, url=f"{SERVER}/{'d' * 64}.png")
    assert UploadResult.from_json(payload, SERVER)["url"] == f"{SERVER}/{SHA}"


def test_a_tokenised_url_for_the_right_blob_is_kept():
    # A signed or tokenised address is spec compliant, and the token can
    # itself be 64 hex characters, so the query is not part of the check.
    tokenised = f"{SERVER}/{SHA}.png?token={'e' * 64}"
    payload = dict(DESCRIPTOR, url=tokenised)
    assert UploadResult.from_json(payload, SERVER)["url"] == tokenised


def test_a_same_origin_url_with_no_hash_in_it_is_kept():
    # Plenty of servers address blobs by an opaque path, and refusing
    # those would break retrieval for no gain.
    opaque = f"{SERVER}/media/opaque-name.png"
    payload = dict(DESCRIPTOR, url=opaque)
    assert UploadResult.from_json(payload, SERVER)["url"] == opaque


def test_list_entries_inherit_the_hash_check():
    entries = [{"sha256": SHA, "url": f"{SERVER}/{'d' * 64}", "size": 1}]
    client, nam = _client([_ok_reply(entries)])
    ok = []
    client.list_for_pubkey(SERVER, PUBKEY, auth_event(SERVER, "list"),
                           ok.append, lambda *_a: None)
    nam.issued[0].finish()
    assert ok[0][0]["url"] == f"{SERVER}/{SHA}"


def test_list_response_urls_are_sanitised_at_parse_time():
    entries = [
        {"sha256": SHA, "url": "file:///etc/passwd", "size": 1},
        {"sha256": "d" * 64, "url": f"{SERVER}/{'d' * 64}", "size": 2},
        {"no_sha": True},
        "not even a dict",
    ]
    client, nam = _client([_ok_reply(entries)])
    ok, err = [], []
    client.list_for_pubkey(SERVER, PUBKEY, auth_event(SERVER, "list"),
                           ok.append, err.append)
    nam.issued[0].finish()
    assert err == []
    payload = ok[0]
    assert payload[0]["url"] == f"{SERVER}/{SHA}"
    assert payload[1]["url"] == f"{SERVER}/{'d' * 64}"
    assert payload[2] == {"no_sha": True}
    assert payload[3] == "not even a dict"


# --------------------------------------------------------------------------- #
# Size caps
# --------------------------------------------------------------------------- #

def test_oversized_list_body_is_aborted_mid_transfer():
    client, nam = _client([_ok_reply([])])
    ok, err = [], []
    client.list_for_pubkey(SERVER, PUBKEY, auth_event(SERVER, "list"),
                           ok.append, err.append)
    reply = nam.issued[0]
    reply.progress(9 * 1024 * 1024, -1)
    assert reply.aborted is True
    reply.finish()
    assert ok == []
    assert err[0].code == ERROR_CODES.TOO_LARGE


def test_announced_oversize_is_refused_before_any_bytes_arrive():
    client, nam = _client([_ok_reply(DESCRIPTOR)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", auth_event(SERVER),
                  ok.append, err.append)
    reply = nam.issued[0]
    reply.progress(0, 4 * 1024 * 1024)
    assert reply.aborted is True
    reply.finish()
    assert ok == []
    assert err[0].code == ERROR_CODES.TOO_LARGE


def test_a_normal_sized_response_is_not_disturbed():
    client, nam = _client([_ok_reply(DESCRIPTOR)])
    ok, err = [], []
    client.upload(SERVER, BODY, "image/png", auth_event(SERVER),
                  ok.append, err.append)
    reply = nam.issued[0]
    reply.progress(200, 200)
    assert reply.aborted is False
    reply.finish()
    assert err == []
    assert ok[0]["hash"] == SHA


# --------------------------------------------------------------------------- #
# Origins
# --------------------------------------------------------------------------- #

def test_server_origin_keeps_ipv6_brackets():
    assert server_origin("http://[::1]:3000/upload") == "http://[::1]:3000"
    assert server_origin("https://Blossom.Band/") == "https://blossom.band"
    with pytest.raises(ValueError):
        server_origin("not a url")


# -- the first bytes of a blob (RG-M2r 5) ----------------------------------------

@pytest.mark.parametrize("url, count", [
    ("http://media.example/" + SHA, 8),          # http off loopback
    ("file:///etc/passwd", 8),
    ("https://user@media.example/" + SHA, 8),
    (f"{SERVER}/{SHA}", 0),
    (f"{SERVER}/{SHA}", 65),                     # more than a head
])
def test_a_head_is_read_only_from_a_media_address_and_only_a_few_bytes(url, count):
    client, nam = _client()
    got, failed = [], []
    client.read_head(url, count, got.append, failed.append)
    assert nam.calls == [] and got == [] and len(failed) == 1


def test_a_head_is_asked_for_by_range_and_never_follows_a_redirect():
    client, nam = _client([redirect_reply(), FakeReply(status=206, body=b"SMF1abcdXYZ")])
    got, failed = [], []
    client.read_head(f"{SERVER}/{SHA}", 8, got.append, failed.append)
    nam.settle()
    assert bytes(nam.calls[0][1].rawHeader("Range")) == b"bytes=0-7"
    assert got == [] and len(failed) == 1 and len(nam.calls) == 1
    client.read_head(f"{SERVER}/{SHA}", 8, got.append, failed.append)
    nam.settle()
    assert got == [b"SMF1abcd"]                   # never more than asked


@pytest.mark.parametrize("status", [404, 416, 500])
def test_a_head_the_server_refuses_is_a_failure(status):
    client, nam = _client([FakeReply(status=status, body=b"SMF1abcd")])
    got, failed = [], []
    client.read_head(f"{SERVER}/{SHA}", 8, got.append, failed.append)
    nam.settle()
    assert got == [] and len(failed) == 1


def test_a_head_read_stopped_on_its_way_is_aborted_and_answers_nothing():
    # RG-M2r3 10: a new listing stops the reads of the old one; Qt ends an
    # aborted reply with finished, which must answer nobody.
    reply = FakeReply(status=206, body=b"SMF1abcd")
    client, nam = _client([reply])
    got, failed = [], []
    stop = client.read_head(f"{SERVER}/{SHA}", 8, got.append, failed.append)
    stop()
    assert reply.aborted and client._inflight == {}
    reply.finish()                                  # as Qt ends an aborted reply
    assert got == [] and failed == []
    stop()                                          # once is enough
    assert client.read_head("file:///etc/passwd", 8, got.append, failed.append) is None


# -- a head read is bounded whatever the server does (RG-M2r2a 4) -------------------

def _head(reply):
    client, nam = _client([reply])
    got, failed = [], []
    client.read_head(f"{SERVER}/{SHA}", 8, got.append, failed.append)
    return reply, got, failed


def test_a_head_read_buffers_no_more_than_it_asks_for():
    reply, got, _failed = _head(FakeReply(status=200, body=b"SMF1abcd" + b"x" * 4096))
    assert reply.read_buffer_size == 8
    reply.finish()
    assert got == [b"SMF1abcd"] and reply.aborted


@pytest.mark.parametrize("status", [404, 500, 503])
def test_a_refusal_is_stopped_at_its_headers_and_its_body_never_read(status):
    reply, got, failed = _head(FakeReply(status=status, body=b"e" * 100_000))
    reply.finish()
    assert reply.aborted and reply._read == 0
    assert got == [] and len(failed) == 1 and failed[0].status == status


def test_a_server_that_keeps_sending_is_stopped_at_the_bound():
    from nostr.blossom import client as module
    reply, got, failed = _head(FakeReply(status=200, body=b"SMF1abcd"))
    reply.progress(module._HEAD_RECEIVED_MAX + 1)
    assert reply.aborted
    reply.finish()
    assert got == [] and len(failed) == 1 and "larger" in str(failed[0])


def test_a_whole_blob_announced_by_a_server_that_ignores_the_range_is_still_read():
    # Its first bytes are what is asked for: the announced length is no bound.
    reply, got, failed = _head(FakeReply(status=200, body=b"SMF1abcd" + b"x" * 64))
    reply.progress(16, 512 * 1024 * 1024)
    assert not reply.aborted
    reply.finish()
    assert got == [b"SMF1abcd"] and failed == []


def test_a_head_read_ends_at_its_deadline_however_slowly_bytes_come():
    # The deadline's timer fires as Qt would fire it (lesson 34).
    from PySide6.QtCore import QTimer
    from nostr.blossom import client as module
    reply, got, failed = _head(FakeReply(status=200, body=b"SMF"))
    deadline = reply.findChild(QTimer)
    assert deadline.isActive() and deadline.interval() == module._HEAD_DEADLINE_MS
    deadline.timeout.emit()
    assert reply.aborted
    reply.finish()
    assert got == [] and len(failed) == 1 and "too long" in str(failed[0])
