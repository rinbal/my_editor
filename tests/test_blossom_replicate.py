# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nostr.blossom.replicate``: the shared put-one-blob-on-one-server pair.

Both the media library and the feed importer go through this module, so
what it signs and sends is pinned here rather than twice over in each
caller's tests.

Nothing here opens a socket or signs: the transport is ``FakeNam``
driven by ``FakeBlossomServer``, which applies BUD-11's validation rules
to us, and the signer is ``FakeSessionPool``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from nostr.blossom import replicate
from nostr.blossom.client import BlossomClient, UploadResult
from nostr.blossom.errors import ERROR_CODES
from tests.blossom_fakes import (
    BODY,
    MIRROR,
    OTHER_SHA,
    SERVER,
    SHA,
    FakeBlossomServer,
    FakeNam,
    FakeProfile,
    FakeSessionPool,
    FakeSigner,
    error_reply,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class Ctx:
    """One wired-up primitive call plus what it produced."""

    def __init__(self, *, responder=None, replies=None, signer=None,
                 pool_error=None, client=None):
        self.nam = FakeNam(replies, responder=responder)
        self.signer = signer or FakeSigner()
        self.pool = FakeSessionPool(self.signer, error=pool_error)
        self.client = client or BlossomClient(nam=self.nam)
        self.results = []
        self.failures = []

    def upload(self, *, server=SERVER, body=BODY, mime="image/png", sha=""):
        replicate.upload_to_server(
            session_pool=self.pool,
            profile=FakeProfile(),
            client=self.client,
            server=server,
            body=body,
            mime=mime,
            sha256=sha,
            on_success=self.results.append,
            on_failure=self.failures.append,
        )

    def mirror(self, *, server=MIRROR, source_url=f"{SERVER}/{SHA}.png",
               sha=SHA):
        replicate.mirror_to_server(
            session_pool=self.pool,
            profile=FakeProfile(),
            client=self.client,
            server=server,
            source_url=source_url,
            sha256=sha,
            on_success=self.results.append,
            on_failure=self.failures.append,
        )

    def token(self, index: int = 0) -> dict:
        return self.signer.requests[index]

    def verbs(self):
        return [verb for verb, _r, _b in self.nam.calls]

    def request(self, index: int = 0):
        return self.nam.calls[index][1]

    def body(self, index: int = 0):
        return self.nam.calls[index][2]


def tag(event, name):
    return [t[1] for t in event["tags"] if t[0] == name]


# --------------------------------------------------------------------------- #
# upload_to_server
# --------------------------------------------------------------------------- #

def test_upload_signs_one_token_scoped_to_one_bare_domain():
    ctx = Ctx(responder=FakeBlossomServer(SERVER))
    ctx.upload()
    ctx.nam.settle()

    assert ctx.pool.calls == 1
    assert len(ctx.signer.requests) == 1
    token = ctx.token()
    assert tag(token, "t") == ["upload"]
    # BUD-11: a bare lowercase domain, never a full URL. The BUD-03
    # kind 10063 `server` tag is the opposite and is pinned separately
    # in tests/test_blossom_server_tags.py.
    assert tag(token, "server") == ["good.example"]
    assert tag(token, "x") == [SHA]
    assert ctx.failures == []
    assert ctx.results[0]["hash"] == SHA


def test_upload_sends_the_x_sha_256_header_matching_the_bytes():
    ctx = Ctx(responder=FakeBlossomServer(SERVER))
    ctx.upload()
    header = bytes(ctx.request().rawHeader("X-SHA-256")).decode("ascii")
    assert header == SHA
    ctx.nam.settle()
    assert ctx.failures == []


def test_upload_hashes_the_body_when_the_caller_did_not():
    """The importer never holds a hash before it downloads the bytes."""
    ctx = Ctx(responder=FakeBlossomServer(SERVER))
    ctx.upload(body=b"other bytes", sha="")
    expected = hashlib.sha256(b"other bytes").hexdigest()
    assert tag(ctx.token(), "x") == [expected]
    assert bytes(ctx.request().rawHeader("X-SHA-256")).decode("ascii") == expected


def test_upload_of_a_descriptor_naming_another_blob_fails():
    ctx = Ctx(responder=FakeBlossomServer(SERVER, sha_override=OTHER_SHA))
    ctx.upload()
    ctx.nam.settle()
    assert ctx.results == []
    assert ctx.failures[0].code == ERROR_CODES.HASH_MISMATCH


def test_upload_reports_a_transport_failure_rather_than_swallowing_it():
    ctx = Ctx(replies=[error_reply(500, reason="disk full")])
    ctx.upload()
    ctx.nam.settle()
    assert ctx.results == []
    assert ctx.failures[0].status == 500


def test_upload_signer_refusal_keeps_its_exact_wording():
    """``nostr/media/manager.py`` classifies signer failures by prefix."""
    ctx = Ctx(signer=FakeSigner(failure="user declined"))
    ctx.upload()
    assert ctx.nam.calls == []
    assert str(ctx.failures[0]) == (
        "signer rejected the Blossom auth event: user declined"
    )
    assert ctx.failures[0].code == ERROR_CODES.SIGNER_REJECTED


# --------------------------------------------------------------------------- #
# mirror_to_server
# --------------------------------------------------------------------------- #

def test_mirror_signs_an_upload_token_carrying_the_blob_hash():
    ctx = Ctx(responder=FakeBlossomServer(MIRROR))
    ctx.mirror()
    ctx.nam.settle()

    token = ctx.token()
    assert tag(token, "t") == ["upload"]
    assert tag(token, "server") == ["mirror.example"]
    # BUD-11's endpoint table: `x` is REQUIRED on PUT /mirror, and its
    # value is the sha256 of the blob being copied.
    assert tag(token, "x") == [SHA]
    assert ctx.failures == []


def test_mirror_body_is_the_bud_04_json_object():
    ctx = Ctx(responder=FakeBlossomServer(MIRROR))
    ctx.mirror()
    assert ctx.request().url().path() == "/mirror"
    assert ctx.body() == json.dumps(
        {"url": f"{SERVER}/{SHA}.png"}, separators=(",", ":")
    ).encode("utf-8")


def test_mirror_never_sends_an_x_sha_256_header():
    """The body is JSON, not the blob, so hashing it describes nothing."""
    ctx = Ctx(responder=FakeBlossomServer(MIRROR))
    ctx.mirror()
    assert bytes(ctx.request().rawHeader("X-SHA-256")) == b""


def test_a_refused_source_url_costs_no_prompt_and_no_request():
    ctx = Ctx()
    ctx.mirror(source_url="http://127.0.0.1:8080/secret.png")
    assert ctx.pool.calls == 0
    assert ctx.nam.calls == []
    assert ctx.failures[0].code == ERROR_CODES.UNSAFE_URL


def test_mirror_of_a_descriptor_naming_another_blob_fails():
    ctx = Ctx(responder=FakeBlossomServer(MIRROR, sha_override=OTHER_SHA))
    ctx.mirror()
    ctx.nam.settle()
    assert ctx.results == []
    assert ctx.failures[0].code == ERROR_CODES.HASH_MISMATCH


def test_mirror_reports_a_refusal_rather_than_swallowing_it():
    ctx = Ctx(replies=[error_reply(502, reason="could not fetch source")])
    ctx.mirror()
    ctx.nam.settle()
    assert ctx.results == []
    assert ctx.failures[0].status == 502


# --------------------------------------------------------------------------- #
# The hash check inside the primitive, with the client's own check out of
# the way
# --------------------------------------------------------------------------- #

class UncheckedClient:
    """A transport that hands back whatever descriptor it was given.

    ``BlossomClient`` refuses a descriptor naming another blob before
    the primitive ever sees it, which is what the two tests above
    exercise; they pass with the primitive's own check removed. This
    stub is the configuration the primitive's check is written for. What
    callers trust is this module, so a client wired in from somewhere
    else, or one that grows a ``PUT /media`` style endpoint where the
    hash legitimately changes, must not be able to turn somebody else's
    blob into a success here.
    """

    def __init__(self, sha: str):
        self.descriptor = UploadResult(
            hash=sha,
            url=f"{SERVER}/{sha}",
            size=len(BODY),
            mime_type="image/png",
            server=SERVER,
            existed=False,
            nip94={},
        )
        self.calls = []

    def upload(self, origin, body, mime, auth_event, *,
               on_success, on_failure, sha256=""):
        self.calls.append(("upload", origin, sha256))
        on_success(self.descriptor)

    def mirror(self, origin, source_url, auth_event, *,
               on_success, on_failure, sha256=""):
        self.calls.append(("mirror", origin, sha256))
        on_success(self.descriptor)


def test_upload_refuses_another_blob_even_when_the_client_did_not_look():
    client = UncheckedClient(OTHER_SHA)
    ctx = Ctx(client=client)
    ctx.upload()
    assert client.calls[0][0] == "upload"
    assert ctx.results == []
    assert ctx.failures[0].code == ERROR_CODES.HASH_MISMATCH


def test_mirror_refuses_another_blob_even_when_the_client_did_not_look():
    client = UncheckedClient(OTHER_SHA)
    ctx = Ctx(client=client)
    ctx.mirror()
    assert client.calls[0][0] == "mirror"
    assert ctx.results == []
    assert ctx.failures[0].code == ERROR_CODES.HASH_MISMATCH


def test_the_unchecked_client_still_succeeds_when_the_hashes_agree():
    """So the two tests above cannot pass because the stub is broken."""
    client = UncheckedClient(SHA)
    ctx = Ctx(client=client)
    ctx.upload()
    ctx.mirror()
    assert ctx.failures == []
    assert [r["hash"] for r in ctx.results] == [SHA, SHA]
