# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the membership sidecar: it lends the association's key only to
requests the association would accept, and never gives the key away.

What must hold:

  Without a key it says so (/status, and "not_configured" on every
  membership call), so MyEditor offers the website instead of an error.

  Only the association's membership endpoints are forwarded. Every call
  but the fee lookup needs a NIP-98 signature naming the association's own
  URL for exactly this request (method, body hash, under a minute old,
  validly signed, used once). Anything else is refused before the key is
  spent.

  What is forwarded carries the key and the user's signature unchanged;
  the answer comes back unchanged, Retry-After included, except that the
  key is removed should the association ever echo it.

  The key's shared quota is protected: requests per address per minute,
  invoices per account per day.

  The key never appears in an answer or in the log.
"""

import asyncio
import base64
import gzip
import json
import logging
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SIDECAR_NO_AUTOSTART"] = "1"

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from nostr import crypto, events, nip98  # noqa: E402
from sidecar import app as sidecar  # noqa: E402

KEY = "association-client-key-0123"
UPSTREAM = "https://verein.einundzwanzig.space"
SK = bytes.fromhex("4d" * 32)
PK = crypto.get_public_key(SK).hex()
NOW = 1_800_000_000


class Clock:
    def __init__(self):
        self.now = float(NOW)

    def __call__(self):
        return self.now


def reply(status=200, *, json_body=None, content=b"", headers=None, declared=True):
    """An answer the way a connection delivers it: streamed, not pre-read
    (``httpx.Response(content=...)`` would hand the sidecar a body it has
    already consumed). ``declared`` sends a Content-Length."""
    headers = dict(headers or {})
    if json_body is not None:
        content = json.dumps(json_body).encode()
        headers.setdefault("Content-Type", "application/json")
    if declared:
        headers.setdefault("Content-Length", str(len(content)))
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(content))


class Association:
    """A fake association behind the sidecar, recording what reached it."""

    def __init__(self):
        self.requests = []
        self.answer = lambda request: reply(json_body={"data": {"ok": True}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


def make(*, key=KEY, per_minute=60, invoices=10):
    association = Association()
    clock = Clock()
    settings = sidecar.Settings(api_key=key, upstream=UPSTREAM,
                                rate_per_minute=per_minute, invoices_per_day=invoices)
    app = sidecar.create_app(settings, transport=httpx.MockTransport(association.handler),
                             clock=clock)
    return TestClient(app), association, clock


def auth(method, path, body=b"", *, url=None, created_at=NOW, sk=SK, tamper=None):
    unsigned = nip98.build_unsigned_auth_event(
        url or f"{UPSTREAM}/api/v1/membership{path}", method, body or None,
        now=created_at)
    event = events.sign_event(unsigned, sk)
    if tamper:
        event = tamper(event)
    return nip98.authorization_header_value(event).decode("ascii")


def call(client, method, path, body=b"", **auth_kw):
    headers = {"Authorization": auth(method, path, body, **auth_kw)}
    if body:
        headers["Content-Type"] = "application/json"
    return client.request(method, f"/api/v1/membership{path}", content=body or None,
                          headers=headers)


# -- availability ------------------------------------------------------------------

def test_status_says_whether_joining_is_possible():
    client, *_ = make()
    assert client.get("/status").json() == {"service": "myeditor-sidecar", "version": "1",
                                            "membership": True}
    client, *_ = make(key="")
    assert client.get("/status").json()["membership"] is False
    assert client.get("/healthz").json() == {"ok": True}


def test_without_a_key_every_call_says_not_configured():
    client, association, _ = make(key="")
    response = call(client, "GET", "/me")
    assert response.status_code == 503 and response.json()["code"] == "not_configured"
    assert association.requests == []


# -- what is forwarded ----------------------------------------------------------------

def test_the_fee_lookup_needs_no_signature_and_is_cached():
    client, association, _ = make()
    first = client.get("/api/v1/membership/config")
    second = client.get("/api/v1/membership/config")
    assert first.status_code == second.status_code == 200
    assert len(association.requests) == 1
    forwarded = association.requests[0]
    assert str(forwarded.url) == f"{UPSTREAM}/api/v1/membership/config"
    assert forwarded.headers["X-Api-Key"] == KEY
    assert "authorization" not in forwarded.headers


def test_the_fee_lookup_is_asked_again_once_the_cache_expires():
    client, association, clock = make()
    client.get("/api/v1/membership/config")
    clock.now += sidecar.CONFIG_CACHE_SECONDS - 1
    client.get("/api/v1/membership/config")
    assert len(association.requests) == 1
    clock.now += 2
    client.get("/api/v1/membership/config")
    assert len(association.requests) == 2


@pytest.mark.parametrize("status", [404, 422, 429, 500, 503])
def test_a_fee_lookup_that_failed_is_not_cached(status):
    client, association, _ = make()
    association.answer = lambda r: reply(status, json_body={"message": "Nope."})
    assert client.get("/api/v1/membership/config").status_code == status
    association.answer = lambda r: reply(json_body={"data": {"fee": 21}})
    response = client.get("/api/v1/membership/config")
    assert response.status_code == 200 and len(association.requests) == 2


def test_a_signed_call_is_forwarded_with_the_key_and_the_signature_unchanged():
    client, association, _ = make()
    body = json.dumps({"statutes_accepted": True}).encode()
    response = call(client, "POST", "/applications", body)
    assert response.status_code == 200
    forwarded = association.requests[0]
    assert str(forwarded.url) == f"{UPSTREAM}/api/v1/membership/applications"
    assert forwarded.headers["X-Api-Key"] == KEY
    assert forwarded.headers["Authorization"].startswith("Nostr ")
    assert forwarded.content == body
    assert forwarded.headers["Content-Type"] == "application/json"


def test_the_answer_comes_back_unchanged_with_its_wait():
    client, association, _ = make()
    association.answer = lambda r: reply(
        429, json_body={"message": "Too Many Attempts."}, headers={"Retry-After": "42"})
    response = call(client, "GET", "/me")
    assert response.status_code == 429 and response.headers["Retry-After"] == "42"
    assert response.json() == {"message": "Too Many Attempts."}


@pytest.mark.parametrize("method, path", [
    ("GET", "/me"), ("DELETE", "/me"), ("GET", "/payments"), ("GET", "/export"),
    ("POST", "/payments/2026/invoice"), ("POST", "/payments/2026/refresh"),
])
def test_every_membership_call_is_forwarded(method, path):
    client, association, _ = make()
    assert call(client, method, path).status_code == 200
    assert str(association.requests[0].url) == f"{UPSTREAM}/api/v1/membership{path}"


@pytest.mark.parametrize("method, path", [
    ("GET", "/admin"), ("POST", "/me"), ("GET", "/payments/26/invoice"),
    ("PUT", "/applications"), ("PATCH", "/me"), ("OPTIONS", "/config"),
    ("GET", "/../secret"), ("GET", "/me?debug=1"), ("GET", "/config?x"),
    ("POST", "/payments/\u0662\u0660\u0662\u0666/invoice"),     # digits, but not 0-9
])
def test_anything_else_is_not_forwarded(method, path):
    client, association, _ = make()
    response = client.request(method, f"/api/v1/membership{path}")
    assert response.status_code == 404
    assert response.json()["code"] == "not_forwarded"
    assert association.requests == []


@pytest.mark.parametrize("method, path", [
    ("POST", "/status"), ("DELETE", "/healthz"), ("GET", "/"), ("GET", "/docs"),
    ("GET", "/api/v1/other"),
])
def test_the_sidecars_own_paths_answer_404_the_same_way(method, path):
    client, *_ = make()
    response = client.request(method, path)
    assert response.status_code == 404 and response.json()["code"] == "not_forwarded"


def test_a_signed_call_with_a_query_string_is_not_forwarded():
    client, association, _ = make()
    headers = {"Authorization": auth("GET", "/me", url=f"{UPSTREAM}/api/v1/membership/me?x=1")}
    response = client.get("/api/v1/membership/me?x=1", headers=headers)
    assert response.status_code == 404 and association.requests == []


# -- what is refused before the key is spent ------------------------------------------

def tampered_sig(event):
    return dict(event, sig=("0" if event["sig"][0] != "0" else "1") + event["sig"][1:])


@pytest.mark.parametrize("kw", [
    {"url": "https://sidecar.example/api/v1/membership/me"},   # names the sidecar
    {"url": f"{UPSTREAM}/api/v1/membership/payments"},          # another endpoint
    {"created_at": NOW - 120},                                  # too old
    {"created_at": NOW + 120},                                  # from the future
    {"tamper": tampered_sig},                                   # forged
])
def test_a_signature_for_anything_but_this_request_is_refused(kw):
    client, association, _ = make()
    response = call(client, "GET", "/me", **kw)
    assert response.status_code == 401 and association.requests == []


def test_a_missing_or_garbled_signature_is_refused():
    client, association, _ = make()
    assert client.get("/api/v1/membership/me").status_code == 401
    garbled = {"Authorization": "Nostr " + base64.b64encode(b"{not json").decode()}
    assert client.get("/api/v1/membership/me", headers=garbled).status_code == 401
    assert association.requests == []


def raw_credential(event) -> str:
    return "Nostr " + base64.b64encode(json.dumps(event).encode()).decode()


def signed_event(method="GET", path="/me", body=None):
    unsigned = nip98.build_unsigned_auth_event(
        f"{UPSTREAM}/api/v1/membership{path}", method, body, now=NOW)
    return events.sign_event(unsigned, SK)


@pytest.mark.parametrize("change", [
    {"tags": None}, {"tags": 5}, {"tags": "u"}, {"tags": [None]}, {"tags": [5]},
    {"tags": [[]]}, {"tags": [["u", 5]]}, {"tags": [["u", None]]},
    {"kind": "27235"}, {"kind": None}, {"created_at": None}, {"created_at": True},
    {"created_at": "1800000000"}, {"content": None}, {"id": 5}, {"pubkey": None},
    {"sig": ["x"]},
])
def test_a_malformed_credential_is_refused_not_a_crash(change):
    client, association, _ = make()
    event = dict(signed_event(), **change)
    response = client.get("/api/v1/membership/me",
                          headers={"Authorization": raw_credential(event)})
    assert response.status_code == 401 and association.requests == []


@pytest.mark.parametrize("document", [None, 5, "event", [], [1, 2]])
def test_a_credential_that_is_not_an_event_is_refused(document):
    client, association, _ = make()
    response = client.get("/api/v1/membership/me",
                          headers={"Authorization": raw_credential(document)})
    assert response.status_code == 401 and association.requests == []


def test_a_tag_named_twice_names_nothing():
    client, association, _ = make()
    unsigned = nip98.build_unsigned_auth_event(
        f"{UPSTREAM}/api/v1/membership/me", "GET", None, now=NOW)
    unsigned["tags"].append(["u", f"{UPSTREAM}/api/v1/membership/me"])
    headers = {"Authorization": raw_credential(events.sign_event(unsigned, SK))}
    assert client.get("/api/v1/membership/me", headers=headers).status_code == 401
    assert association.requests == []


def test_a_payload_tag_without_a_body_is_refused():
    client, association, _ = make()
    event = signed_event("POST", "/payments/2026/refresh", b'{"a":1}')
    response = client.post("/api/v1/membership/payments/2026/refresh",
                           headers={"Authorization": raw_credential(event)})
    assert response.status_code == 401 and association.requests == []


def test_the_method_must_match():
    client, association, _ = make()
    headers = {"Authorization": auth("GET", "/me")}
    assert client.delete("/api/v1/membership/me", headers=headers).status_code == 401
    assert association.requests == []


def test_the_body_must_be_the_one_that_was_signed():
    client, association, _ = make()
    headers = {"Authorization": auth("POST", "/applications", b'{"a":1}'),
               "Content-Type": "application/json"}
    response = client.post("/api/v1/membership/applications", content=b'{"a":2}',
                           headers=headers)
    assert response.status_code == 401 and association.requests == []


def test_a_signature_is_used_once():
    client, association, _ = make()
    headers = {"Authorization": auth("GET", "/me")}
    assert client.get("/api/v1/membership/me", headers=headers).status_code == 200
    assert client.get("/api/v1/membership/me", headers=headers).status_code == 401
    assert len(association.requests) == 1


def test_a_body_must_be_json_and_small():
    client, association, _ = make()
    body = b'{"statutes_accepted": true}'
    headers = {"Authorization": auth("POST", "/applications", body),
               "Content-Type": "text/plain"}
    assert client.post("/api/v1/membership/applications", content=body,
                       headers=headers).status_code == 415
    big = b'{"x":"' + b"a" * (40 * 1024) + b'"}'
    assert call(client, "POST", "/applications", big).status_code == 413
    assert association.requests == []


def asgi_post(app, path, chunks, headers=()):
    """POST straight through ASGI, recording which body chunks the app
    actually pulled. Returns (status, chunks read)."""
    pending, read, sent = list(chunks), [], []

    async def receive():
        if pending:
            chunk = pending.pop(0)
            read.append(chunk)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "client": ("192.0.2.1", 4242),
        "server": ("testserver", 80),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, read


def test_a_body_declared_too_large_is_refused_unread():
    client, association, _ = make()
    status, read = asgi_post(client.app, "/api/v1/membership/applications",
                             [b"a" * 1024] * 3,
                             headers=[("Content-Length", str(10 ** 9)),
                                      ("Content-Type", "application/json")])
    assert status == 413 and read == [] and association.requests == []


def test_an_undeclared_body_is_read_only_until_it_passes_the_cap():
    client, association, _ = make()
    chunk = b"a" * (sidecar.MAX_BODY_BYTES // 2)
    status, read = asgi_post(client.app, "/api/v1/membership/applications",
                             [chunk] * 10, headers=[("Content-Type", "application/json")])
    assert status == 413 and len(read) == 3 and association.requests == []


@pytest.mark.parametrize("method, path", [("GET", "/me"), ("DELETE", "/me"),
                                          ("GET", "/config")])
def test_get_and_delete_carry_no_body(method, path):
    client, association, _ = make()
    headers = {"Authorization": auth(method, path), "Content-Type": "application/json"}
    response = client.request(method, f"/api/v1/membership{path}", content=b"{}",
                              headers=headers)
    assert response.status_code == 400 and association.requests == []


@pytest.mark.parametrize("method, path", [("GET", "/me"), ("DELETE", "/me"),
                                          ("GET", "/config")])
def test_get_and_delete_carry_no_content_type(method, path):
    client, association, _ = make()
    headers = {"Authorization": auth(method, path), "Content-Type": "application/json"}
    response = client.request(method, f"/api/v1/membership{path}", headers=headers)
    assert response.status_code == 415 and association.requests == []


# -- answers are read under a cap ------------------------------------------------------

def test_the_association_is_asked_for_an_uncompressed_answer():
    client, association, _ = make()
    call(client, "GET", "/me")
    assert association.requests[0].headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("declared", [True, False])
def test_an_oversized_answer_is_a_502(declared):
    client, association, _ = make()
    big = b'{"data":"' + b"a" * sidecar.MAX_ANSWER_BYTES + b'"}'
    association.answer = lambda r: reply(content=big, declared=declared)
    response = call(client, "GET", "/me")
    assert response.status_code == 502 and "too large" in response.json()["message"]


def test_a_compressed_answer_is_unpacked_and_passed_on():
    client, association, _ = make()
    document = b'{"data":{"ok":true}}'
    association.answer = lambda r: reply(
        content=gzip.compress(document),
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
    response = call(client, "GET", "/me")
    assert response.status_code == 200 and response.content == document


def test_a_compressed_answer_cannot_unpack_past_the_cap():
    client, association, _ = make()
    bomb = gzip.compress(b"0" * (sidecar.MAX_ANSWER_BYTES * 8))
    assert len(bomb) < sidecar.MAX_ANSWER_BYTES // 10
    association.answer = lambda r: reply(content=bomb, headers={"Content-Encoding": "gzip"},
                                         declared=False)
    response = call(client, "GET", "/me")
    assert response.status_code == 502 and "too large" in response.json()["message"]


@pytest.mark.parametrize("encoding, content", [("br", b"\x0b\x02\x80{}\x03"),
                                               ("gzip", b"not gzip at all")])
def test_an_answer_that_cannot_be_unpacked_is_a_502(encoding, content):
    client, association, _ = make()
    association.answer = lambda r: reply(content=content, headers={"Content-Encoding": encoding})
    assert call(client, "GET", "/me").status_code == 502


# -- the shared quota ------------------------------------------------------------------

def test_requests_per_address_are_limited():
    client, association, clock = make(per_minute=3)
    codes = [call(client, "GET", "/me").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    clock.now += 61
    assert call(client, "GET", "/me", created_at=int(clock.now)).status_code == 200


def test_clients_not_seen_for_a_minute_are_forgotten():
    client, association, clock = make()
    limits = client.app.state.limits
    for n in range(50):
        assert limits.allow_request(f"198.51.100.{n}") is None
    assert limits.tracked_clients == 50
    clock.now += 61
    assert limits.allow_request("203.0.113.9") is None
    assert limits.tracked_clients == 1


@pytest.mark.parametrize("host, bucket", [
    ("192.0.2.7", "192.0.2.7"),
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("::ffff:192.0.2.7", "192.0.2.7"),
    ("testclient", "testclient"),
])
def test_an_ipv6_client_counts_by_its_64(host, bucket):
    assert sidecar.client_bucket(host) == bucket


def test_one_ipv6_network_shares_one_limit():
    client, association, clock = make(per_minute=2)
    limits = client.app.state.limits
    assert limits.allow_request("2001:db8::1") is None
    assert limits.allow_request("2001:db8::2") is None
    assert limits.allow_request("2001:db8::ffff:3") is not None
    assert limits.allow_request("2001:db8:0:1::1") is None      # the next /64


def test_the_limit_counts_the_address_the_proxy_reports():
    # Behind Caddy or nginx, uvicorn's --proxy-headers makes the forwarded
    # address the client; this is that middleware in front of the app.
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    association = Association()
    app = sidecar.create_app(
        sidecar.Settings(api_key=KEY, upstream=UPSTREAM, rate_per_minute=1),
        transport=httpx.MockTransport(association.handler), clock=Clock())
    proxied = TestClient(ProxyHeadersMiddleware(app, trusted_hosts="*"))

    def from_address(address):
        headers = {"Authorization": auth("GET", "/me"), "X-Forwarded-For": address}
        return proxied.get("/api/v1/membership/me", headers=headers).status_code

    assert from_address("203.0.113.1") == 200
    assert from_address("203.0.113.2") == 200
    assert from_address("203.0.113.1") == 429
    assert from_address("2001:db8::1") == 200
    assert from_address("2001:db8::2") == 429


def test_invoices_per_account_are_limited_per_day():
    client, association, clock = make(invoices=2)
    codes = [call(client, "POST", "/payments/2026/invoice").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    clock.now += 86400
    assert call(client, "POST", "/payments/2026/invoice",
                created_at=int(clock.now)).status_code == 200


# -- the key stays here ----------------------------------------------------------------

def test_the_key_never_leaves_in_an_answer(caplog):
    client, association, _ = make()
    association.answer = lambda r: reply(
        422, json_body={"message": f"Unknown client key {KEY}"})
    caplog.set_level("INFO", logger="myeditor-sidecar")
    response = call(client, "GET", "/me")
    assert KEY not in response.text and "[redacted]" in response.text
    for path in ("/me", "/payments"):
        call(client, "GET", path, tamper=tampered_sig)
    assert KEY not in caplog.text


# Shaped like a base64 key: slashes, a plus, padding.
SLASHED_KEY = "e21/client+key/0123=="


@pytest.mark.parametrize("echo", [
    SLASHED_KEY,                                    # as is
    "e21\\/client+key\\/0123==",                    # in JSON, the way PHP writes it
    "e21%2Fclient%2Bkey%2F0123%3D%3D",              # percent-encoded
])
def test_the_key_is_removed_in_every_form_it_could_be_echoed_in(echo):
    association = Association()
    app = sidecar.create_app(sidecar.Settings(api_key=SLASHED_KEY, upstream=UPSTREAM),
                             transport=httpx.MockTransport(association.handler),
                             clock=Clock())
    raw = b'{"message":"Unknown client key ' + echo.encode("utf-8") + b'"}'
    association.answer = lambda r: reply(422, content=raw,
                                         headers={"Content-Type": "application/json"})
    response = call(TestClient(app), "GET", "/me")
    assert echo.encode("utf-8") not in response.content
    assert b"[redacted]" in response.content


def test_the_settings_never_print_the_key():
    settings = sidecar.Settings(api_key=KEY, upstream=UPSTREAM)
    assert KEY not in repr(settings) and KEY not in str(settings)


def test_a_refused_key_is_this_servers_problem_not_the_users(caplog):
    client, association, _ = make()
    association.answer = lambda r: reply(401, json_body={"message": "Unauthenticated."})
    caplog.set_level("INFO", logger="myeditor-sidecar")
    response = call(client, "GET", "/me")
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_refused"
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("refused the key or clocks differ" in r.getMessage() for r in warnings)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_fee_lookup_is_the_same_and_is_not_cached(status):
    client, association, _ = make()
    association.answer = lambda r: reply(status, json_body={"message": "Forbidden."})
    response = client.get("/api/v1/membership/config")
    assert response.status_code == 503 and response.json()["code"] == "upstream_refused"
    association.answer = lambda r: reply(json_body={"data": {"fee": 21}})
    assert client.get("/api/v1/membership/config").json() == {"data": {"fee": 21}}
    assert len(association.requests) == 2


def test_a_403_on_a_signed_call_passes_through():
    client, association, _ = make()
    association.answer = lambda r: reply(403, json_body={"message": "Forbidden."})
    response = call(client, "GET", "/me")
    assert response.status_code == 403 and response.json() == {"message": "Forbidden."}


def test_the_http_client_does_not_log_its_requests():
    make()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_importing_the_sidecar_configures_no_logging():
    # The tests import it with SIDECAR_NO_AUTOSTART=1; so does any tool
    # that only wants create_app(). Neither may get a root handler.
    script = ("import logging, os; os.environ['SIDECAR_NO_AUTOSTART'] = '1'; "
              "import sidecar.app as a; "
              "assert a.app is None and not logging.getLogger().handlers")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, "-c", script], cwd=root, check=True)


def test_the_key_is_in_no_log_record_not_even_uvicorns_or_the_http_clients(caplog):
    # The real server, with every logger at DEBUG and uvicorn's access log
    # on (the worst case), through a refused signature, an echoed key, a
    # refused key and an unreachable association.
    uvicorn = pytest.importorskip("uvicorn")
    association = Association()
    app = sidecar.create_app(sidecar.Settings(api_key=KEY, upstream=UPSTREAM),
                             transport=httpx.MockTransport(association.handler))
    for name in ("", "httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access",
                 "myeditor-sidecar"):
        caplog.set_level(logging.DEBUG, logger=name)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None,
                                           log_level="debug", access_log=True))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline and thread.is_alive()
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    def down(request):
        raise httpx.ConnectError(f"down {request.headers['X-Api-Key']}", request=request)

    answers = [
        lambda r: reply(json_body={"data": {"echo": KEY}}),
        lambda r: reply(401, json_body={"message": f"Unknown client key {KEY}"}),
        down,
    ]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as http:
            http.get("/status")
            http.get("/api/v1/membership/config")
            for answer in answers:
                association.answer = answer
                http.get("/api/v1/membership/me",
                         headers={"Authorization": auth("GET", "/me",
                                                        created_at=int(time.time()))})
            http.get("/api/v1/membership/me",
                     headers={"Authorization": auth("GET", "/me", created_at=int(time.time()),
                                                    tamper=tampered_sig)})
    finally:
        server.should_exit = True
        thread.join(10)
    names = {record.name for record in caplog.records}
    assert "uvicorn.access" in names and "myeditor-sidecar" in names
    assert KEY not in caplog.text
    assert all(KEY not in str(record.args) for record in caplog.records)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _lines(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def test_the_key_file_stays_out_of_git_and_out_of_the_image_build():
    # sidecar/.env holds the key. Git ignores it, and the Docker build
    # context (the repository root) lets through exactly the files the
    # Dockerfile copies, so neither .env nor anything else reaches the
    # daemon, and the build still has everything it copies.
    assert "sidecar/.env" in _lines(".gitignore")
    ignore = _lines("sidecar/Dockerfile.dockerignore")
    assert ignore[0] == "*" and all(line.startswith("!") for line in ignore[1:])
    allowed = {line[1:] for line in ignore[1:]}
    copied = set()
    for line in _lines("sidecar/Dockerfile"):
        if line.startswith("COPY "):
            copied.update(line.split()[1:-1])
    assert copied == allowed
    assert not any(path.endswith(".env") or "/." in path for path in allowed)


def test_an_unreachable_association_is_a_clear_502():
    def down(request):
        raise httpx.ConnectError("down", request=request)
    client, association, _ = make()
    association.answer = down
    response = call(client, "GET", "/me")
    assert response.status_code == 502 and "not reachable" in response.json()["message"]


def test_a_redirect_from_upstream_is_not_followed():
    client, association, _ = make()
    association.answer = lambda r: reply(302, headers={"Location": "https://evil.example"})
    response = call(client, "GET", "/me")
    assert response.status_code == 502 and len(association.requests) == 1
