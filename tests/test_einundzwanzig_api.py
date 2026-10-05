# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the client for the EINUNDZWANZIG membership API.

The server is a third party and the user's signer is a phone, so most
of what can go wrong happens somewhere this process does not control.
The cases below are those places: a refused key, a credential that
expired while the user found their phone, a quota, a malformed or
enormous answer, a signer that says no or never answers. Each must end
in exactly one callback carrying a stable code, with no exception
reaching Qt.

The counterparty is ``FakeMembershipServer``, which re-checks every
NIP-98 rule the real server enforces, so "the request was accepted" in
these tests means the credential was right, not merely present.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest

import nostr.einundzwanzig_api as e21
from nostr.einundzwanzig_api import (
    BASE_URL,
    MAX_RESPONSE_BYTES,
    NETWORK_TIMEOUT_MS,
    SIGN_TIMEOUT_MS,
    UNSET,
    ApiError,
    Erasure,
    ErrorCode,
    FeeEntry,
    Invoice,
    MembershipApi,
    MembershipConfig,
    MembershipExport,
    MembershipStatus,
    parse_config,
    parse_erasure,
    parse_export,
    parse_invoice,
    parse_membership,
    parse_payments,
    parse_retry_after,
    service_url,
    session_signer,
)
from tests.membership_fakes import (
    PREFIX,
    SERVICE,
    SERVICE_PREFIX,
    PUBKEY,
    FakeClock,
    FakeMembershipServer,
    FakeNam,
    FakeReply,
    FakeSigner,
    config_data,
    decode_authorization,
    header,
    invoice_data,
    json_reply,
    membership_data,
    tag,
    transport_failure,
)


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    yield app


@pytest.fixture(autouse=True)
def production_association(monkeypatch):
    # A developer's shell may point the app at a test association.
    monkeypatch.delenv("MYEDITOR_MEMBERSHIP_UPSTREAM", raising=False)


def make(*, script=None, signer=None, service=SERVICE, clock=None, server=True):
    clock = clock or FakeClock()
    srv = FakeMembershipServer(clock)
    nam = FakeNam(script, responder=srv if server else None)
    signer = signer if signer is not None else FakeSigner(clock=clock)
    api = MembershipApi(signer, service_url=service, nam=nam, clock=clock)
    return SimpleNamespace(api=api, nam=nam, signer=signer, server=srv, clock=clock)


def run(env, method, *args, **kwargs):
    """Call ``method``, settle the transport, return (successes, failures)."""
    ok, failed = [], []
    getattr(env.api, method)(*args, ok.append, failed.append, **kwargs)
    env.nam.settle()
    return ok, failed


def failure(env, method="me", *args, **kwargs) -> ApiError:
    ok, failed = run(env, method, *args, **kwargs)
    assert ok == [] and len(failed) == 1, (ok, failed)
    assert isinstance(failed[0], ApiError)
    return failed[0]


def sent_body(env, index=-1):
    return env.nam.calls[index][2]


def sent_event(env, index=-1) -> dict:
    return decode_authorization(header(env.nam.calls[index][1], "Authorization"))


# Every call on the surface, with the arguments it needs and the request
# it must produce. Used wherever a property must hold for all of them.
SIGNED_CALLS = [
    ("me", (), {}, "GET", "/me", MembershipStatus),
    ("apply", (), {}, "POST", "/applications", MembershipStatus),
    ("create_invoice", (2026,), {}, "POST", "/payments/2026/invoice", Invoice),
    ("refresh_payment", (2026,), {}, "POST", "/payments/2026/refresh", Invoice),
    ("payments", (), {}, "GET", "/payments", tuple),
    ("export_data", (), {}, "GET", "/export", MembershipExport),
    ("erase", (), {}, "DELETE", "/me", Erasure),
]
ALL_CALLS = [("config", (), {}, "GET", "/config", MembershipConfig)] + SIGNED_CALLS


# --------------------------------------------------------------------- #
# The membership service                                                #
# --------------------------------------------------------------------- #

def test_the_environment_names_the_service(monkeypatch):
    import constants
    monkeypatch.setattr(constants, "MEMBERSHIP_SERVICE_URL", "https://official.example")
    monkeypatch.setenv("MYEDITOR_MEMBERSHIP_SERVICE", "https://mine.example/")
    assert service_url() == "https://mine.example"
    api = MembershipApi(FakeSigner(), nam=FakeNam())
    assert api.configured and api.request_url_for("/me") == (
        "https://mine.example/api/v1/membership/me")


def test_the_build_names_the_service_without_an_environment_value(monkeypatch):
    import constants
    monkeypatch.delenv("MYEDITOR_MEMBERSHIP_SERVICE", raising=False)
    monkeypatch.setattr(constants, "MEMBERSHIP_SERVICE_URL", "https://official.example")
    assert service_url() == "https://official.example"
    monkeypatch.setattr(constants, "MEMBERSHIP_SERVICE_URL", "")
    assert service_url() == ""
    assert not MembershipApi(FakeSigner(), nam=FakeNam()).configured


@pytest.mark.parametrize("value, expected", [
    ("https://e21.example/", "https://e21.example"),
    ("https://e21.example:8443/sidecar", "https://e21.example:8443/sidecar"),
    ("HTTPS://e21.example", "HTTPS://e21.example"),
    ("http://localhost:8021", "http://localhost:8021"),     # development
    ("http://LOCALHOST:8021/", "http://LOCALHOST:8021"),
    ("http://127.0.0.1:8021", "http://127.0.0.1:8021"),
    ("http://[::1]:8021", "http://[::1]:8021"),
    ("http://e21.example", ""),                              # never plain http on the net
    ("http://localhost.evil.example", ""),                   # not this computer
    ("http://127.0.0.1.evil.example", ""),
    ("http://localhost@evil.example", ""),                   # evil.example, with a user name
    ("http://localhost:8021@evil.example", ""),
    ("https://user:secret@e21.example", ""),                 # no credentials in the address
    ("http://localhost:8021?next=https://evil.example", ""), # no query
    ("https://e21.example/?x=1", ""),
    ("https://e21.example/#top", ""),                        # no fragment
    ("http://localhost:8021#", ""),
    ("https://e21.example/a b", ""),
    ("http://localhost:99999", ""),                          # not a port
    ("https://", ""),
    ("https:e21.example", ""),
    ("ftp://e21.example", ""),
    ("not a url", ""),
    ("  ", ""),
])
def test_only_https_services_count(monkeypatch, value, expected):
    monkeypatch.setenv("MYEDITOR_MEMBERSHIP_SERVICE", value)
    import constants
    monkeypatch.setattr(constants, "MEMBERSHIP_SERVICE_URL", "")
    assert service_url() == expected


def test_the_service_is_asked_whether_it_can_sign_people_up():
    env = make()
    answers = []
    env.api.check_service(answers.append)
    env.nam.settle()
    assert answers == [True]
    env.server.status = {"service": "myeditor-sidecar", "membership": False}
    env.api.check_service(answers.append)
    env.nam.settle()
    assert answers == [True, False]


def test_no_service_means_not_available_without_asking():
    env = make(service="")
    answers = []
    env.api.check_service(answers.append)
    QCoreApplication.processEvents()
    assert answers == [False] and env.nam.calls == []


@pytest.mark.parametrize("service_code", ["not_configured", "upstream_refused"])
def test_a_service_without_a_usable_key_means_joining_is_unavailable(service_code):
    # No key on the service, or the association refused it: nothing the
    # user can do about either, so not "could not confirm it is you".
    script = [json_reply({"message": "Not available.", "code": service_code}, status=503)]
    env = make(script=script, server=False)
    assert failure(env, "config").code == ErrorCode.UNAVAILABLE
    env = make(script=[json_reply({"message": "Not available.", "code": service_code},
                                  status=503)], server=False)
    error = failure(env, "me")
    assert error.code == ErrorCode.UNAVAILABLE and len(env.signer.requests) == 1


def test_another_503_is_the_servers_trouble():
    env = make(script=[json_reply({"message": "Down.", "code": "maintenance"}, status=503)],
               server=False)
    assert failure(env, "me").code == ErrorCode.SERVER


def test_the_services_own_404_is_not_nothing_on_record():
    script = [json_reply({"message": "Not Found", "code": "not_forwarded"}, status=404)]
    env = make(script=script, server=False)
    assert failure(env, "me").code == ErrorCode.BAD_RESPONSE


@pytest.mark.parametrize("name, args, kwargs, verb, path, kind", ALL_CALLS)
def test_without_a_service_nothing_is_signed_or_sent(name, args, kwargs, verb, path, kind):
    env = make(service="")
    error = failure(env, name, *args, **kwargs)
    assert error.code == ErrorCode.UNAVAILABLE
    assert env.signer.requests == []
    assert env.nam.calls == []


# --------------------------------------------------------------------- #
# Request hygiene                                                       #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("name, args, kwargs, verb, path, kind", ALL_CALLS)
def test_every_call_reaches_its_documented_endpoint(name, args, kwargs, verb, path, kind):
    env = make()
    ok, failed = run(env, name, *args, **kwargs)
    assert failed == [] and isinstance(ok[0], kind)
    called_verb, request, _body = env.nam.calls[0]
    assert called_verb == verb
    assert request.url().toString() == SERVICE_PREFIX + path     # travels to the service
    assert env.server.refusals == []


@pytest.mark.parametrize("name, args, kwargs, verb, path, kind", ALL_CALLS)
def test_no_request_carries_a_key_and_none_can_hang_or_wander(name, args, kwargs, verb, path, kind):
    env = make()
    run(env, name, *args, **kwargs)
    request = env.nam.calls[0][1]
    assert header(request, "X-Api-Key") is None          # the key lives on the service only
    assert header(request, "Accept") == b"application/json"
    assert request.transferTimeout() == NETWORK_TIMEOUT_MS
    assert request.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute) == (
        QNetworkRequest.RedirectPolicy.SameOriginRedirectPolicy
    )
    assert 0 < request.maximumRedirectsAllowed() <= 5


def test_the_production_server_is_the_documented_one():
    assert BASE_URL == "https://verein.einundzwanzig.space"
    assert MembershipApi(FakeSigner(), service_url=SERVICE, nam=FakeNam()).url_for("/me") == (
        "https://verein.einundzwanzig.space/api/v1/membership/me"
    )


def test_signatures_name_the_production_association_unless_a_developer_says_otherwise(
        monkeypatch):
    monkeypatch.delenv("MYEDITOR_MEMBERSHIP_UPSTREAM", raising=False)
    assert e21.upstream_url() == BASE_URL
    api = MembershipApi(FakeSigner(), service_url=SERVICE, nam=FakeNam())
    assert api.url_for("/me") == BASE_URL + "/api/v1/membership/me"

    monkeypatch.setenv("MYEDITOR_MEMBERSHIP_UPSTREAM", "http://localhost:8000/")
    api = MembershipApi(FakeSigner(), service_url=SERVICE, nam=FakeNam())
    assert api.url_for("/me") == "http://localhost:8000/api/v1/membership/me"
    assert api.request_url_for("/me") == SERVICE_PREFIX + "/me"     # still travels there


@pytest.mark.parametrize("value", ["http://test.example", "http://localhost@evil.example",
                                   "https://test.example/?x", "not a url", ""])
def test_an_unusable_upstream_override_is_ignored(monkeypatch, value):
    monkeypatch.setenv("MYEDITOR_MEMBERSHIP_UPSTREAM", value)
    assert e21.upstream_url() == BASE_URL


def test_an_explicit_base_url_wins_over_the_override(monkeypatch):
    monkeypatch.setenv("MYEDITOR_MEMBERSHIP_UPSTREAM", "https://test.example")
    api = MembershipApi(FakeSigner(), service_url=SERVICE, base_url="https://other.example",
                        nam=FakeNam())
    assert api.url_for("/me") == "https://other.example/api/v1/membership/me"


def test_a_trailing_slash_on_the_base_url_does_not_double():
    api = MembershipApi(FakeSigner(), service_url=SERVICE, base_url="http://localhost:8000/", nam=FakeNam())
    assert api.url_for("/me") == "http://localhost:8000/api/v1/membership/me"


def test_a_relative_base_url_is_refused():
    with pytest.raises(ValueError):
        MembershipApi(FakeSigner(), service_url=SERVICE, base_url="/api", nam=FakeNam())


# --------------------------------------------------------------------- #
# Configuration needs no signature                                      #
# --------------------------------------------------------------------- #

def test_config_is_fetched_without_a_signature():
    env = make()
    ok, failed = run(env, "config")
    assert failed == []
    assert env.signer.requests == []
    request = env.nam.calls[0][1]
    assert header(request, "Authorization") is None
    assert ok[0] == MembershipConfig(
        fee=21, currency="CHF", year=2026,
        statutes_url="https://einundzwanzig.space/files/Statuten_v1.3.pdf",
        statutes_version="1.3", statutes_adopted_at="2024-04-20",
        required_fields=("statutes_accepted",),
        optional_fields=("application_text", "email", "no_email", "nip05_handle"),
        application_text_max_length=2000,
    )


# --------------------------------------------------------------------- #
# Signed calls                                                          #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("name, args, kwargs, verb, path, kind", SIGNED_CALLS)
def test_every_signed_call_carries_a_credential_for_exactly_that_request(
    name, args, kwargs, verb, path, kind,
):
    env = make()
    run(env, name, *args, **kwargs)
    event = sent_event(env)
    assert event["kind"] == 27235
    assert tag(event, "u") == PREFIX + path
    assert tag(event, "method") == verb
    assert event["created_at"] == int(env.clock())
    assert env.server.accepted and env.server.refusals == []


def test_each_call_is_signed_fresh_even_for_the_same_request():
    # Ids are accepted once. Two identical calls in the same second must
    # still produce two different credentials.
    env = make()
    run(env, "me")
    run(env, "me")
    assert len(env.signer.requests) == 2
    first, second = sent_event(env, 0), sent_event(env, 1)
    assert first["id"] != second["id"]
    assert tag(first, "nonce") != tag(second, "nonce")
    assert env.server.refusals == []


def test_the_event_is_built_when_the_call_is_made():
    env = make()
    env.clock.advance(1234)
    run(env, "me")
    assert env.signer.requests[0]["created_at"] == int(env.clock())


@pytest.mark.parametrize("name, args, verb", [
    ("me", (), "GET"),
    ("payments", (), "GET"),
    ("export_data", (), "GET"),
    ("refresh_payment", (2026,), "POST"),
    ("erase", (), "DELETE"),
])
def test_a_request_without_a_body_has_no_content_type_and_no_payload(name, args, verb):
    env = make()
    run(env, name, *args)
    called_verb, request, body = env.nam.calls[0]
    assert called_verb == verb
    assert not body
    assert header(request, "Content-Type") is None
    assert tag(sent_event(env), "payload") is None


def test_a_json_body_is_hashed_and_sent_as_the_same_bytes():
    env = make()
    run(env, "apply", email="satoshi@example.org")
    _verb, request, body = env.nam.calls[0]
    assert header(request, "Content-Type") == b"application/json"
    import hashlib
    assert tag(sent_event(env), "payload") == hashlib.sha256(body).hexdigest()
    assert env.server.refusals == []


def test_an_invoice_without_a_return_address_sends_no_body():
    env = make()
    run(env, "create_invoice", 2026)
    assert not sent_body(env)
    assert header(env.nam.calls[0][1], "Content-Type") is None


def test_an_invoice_with_a_return_address_sends_only_that():
    env = make()
    run(env, "create_invoice", 2026, return_url="https://example.org/back")
    assert json.loads(sent_body(env)) == {"return_url": "https://example.org/back"}
    assert env.server.refusals == []


@pytest.mark.parametrize("year", [26, 20260, "2026", None, True])
def test_a_year_that_is_not_four_digits_is_a_programming_error(year):
    env = make()
    with pytest.raises(ValueError):
        env.api.create_invoice(year, lambda _v: None, lambda _e: None)
    with pytest.raises(ValueError):
        env.api.refresh_payment(year, lambda _v: None, lambda _e: None)


# --------------------------------------------------------------------- #
# Applying: omitted is untouched, None is cleared                       #
# --------------------------------------------------------------------- #

def applied_body(**kwargs) -> dict:
    env = make()
    ok, failed = run(env, "apply", **kwargs)
    assert failed == [], failed
    return json.loads(sent_body(env))


def test_a_plain_application_sends_the_consent_alone():
    assert applied_body() == {"statutes_accepted": True}


def test_only_the_fields_passed_are_sent():
    assert applied_body(email="satoshi@example.org", nip05_handle="satoshi") == {
        "statutes_accepted": True,
        "email": "satoshi@example.org",
        "nip05_handle": "satoshi",
    }


def test_none_is_sent_as_null_to_clear_a_field():
    body = applied_body(email=None, application_text=None, nip05_handle=None)
    assert body == {
        "statutes_accepted": True, "email": None, "application_text": None, "nip05_handle": None,
    }


def test_a_repeat_application_can_leave_the_consent_out():
    assert applied_body(statutes_accepted=UNSET, no_email=True) == {"no_email": True}


def test_no_email_false_is_sent_not_dropped():
    assert applied_body(no_email=False) == {"statutes_accepted": True, "no_email": False}


@pytest.mark.parametrize("kwargs", [
    {"statutes_accepted": False},
    {"statutes_accepted": "yes"},
    {"statutes_accepted": None},
    {"no_email": None},
    {"no_email": "true"},
    {"email": 42},
    {"application_text": ["x"]},
])
def test_misuse_of_apply_is_a_programming_error(kwargs):
    env = make()
    with pytest.raises(TypeError):
        env.api.apply(lambda _v: None, lambda _e: None, **kwargs)
    assert env.signer.requests == [] and env.nam.calls == []


@pytest.mark.parametrize("kwargs, field", [
    ({"nip05_handle": "Satoshi"}, "nip05_handle"),
    ({"nip05_handle": "sat oshi"}, "nip05_handle"),
    ({"nip05_handle": ""}, "nip05_handle"),
    ({"email": "not an address"}, "email"),
    ({"application_text": "x" * 2001}, "application_text"),
])
def test_a_field_the_server_would_refuse_never_reaches_the_signer(kwargs, field):
    # Every signature can be a prompt on the user's phone. A request that
    # is certain to be refused must not cost one.
    env = make()
    error = failure(env, "apply", **kwargs)
    assert error.code == ErrorCode.VALIDATION
    assert list(error.field_errors) == [field]
    assert error.field_errors[field][0]
    assert env.signer.requests == [] and env.nam.calls == []


def test_the_longest_allowed_application_text_is_sent():
    assert applied_body(application_text="x" * 2000)["application_text"] == "x" * 2000


def test_no_service_outranks_a_field_problem():
    env = make(service="")
    assert failure(env, "apply", nip05_handle="BAD").code == ErrorCode.UNAVAILABLE


# --------------------------------------------------------------------- #
# Error mapping                                                         #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("status, code", [
    (401, ErrorCode.UNAUTHORIZED),
    (403, ErrorCode.UNAUTHORIZED),
    (404, ErrorCode.NOT_FOUND),
    (409, ErrorCode.CONFLICT),
    (415, ErrorCode.BAD_RESPONSE),
    (400, ErrorCode.BAD_RESPONSE),
    (405, ErrorCode.BAD_RESPONSE),
    (500, ErrorCode.SERVER),
    (502, ErrorCode.SERVER),
    (503, ErrorCode.SERVER),
])
def test_http_refusals_map_to_stable_codes(status, code):
    env = make(script=[json_reply({"message": "Nope."}, status=status)])
    error = failure(env, "me")
    assert error.code == code
    assert error.status == status
    assert error.message == "Nope."


def test_a_validation_refusal_carries_its_field_errors():
    env = make(script=[json_reply({
        "message": "The nip05 handle has already been taken.",
        "errors": {
            "nip05_handle": ["The nip05 handle has already been taken."],
            "email": "The email must be a valid email address.",
            "junk": 42,
        },
    }, status=422)])
    error = failure(env, "apply", nip05_handle="satoshi")
    assert error.code == ErrorCode.VALIDATION and error.status == 422
    assert error.field_errors == {
        "nip05_handle": ["The nip05 handle has already been taken."],
        "email": ["The email must be a valid email address."],
    }


def test_a_validation_refusal_without_errors_is_still_validation():
    env = make(script=[json_reply({"message": "Bad."}, status=422)])
    error = failure(env, "create_invoice", 2026, return_url="https://elsewhere.example")
    assert error.code == ErrorCode.VALIDATION and error.field_errors == {}


def test_a_quota_refusal_says_how_long_to_wait():
    env = make(script=[json_reply({"message": "Too Many Attempts."}, status=429,
                                  headers={"Retry-After": "30"})])
    error = failure(env, "create_invoice", 2026)
    assert error.code == ErrorCode.RATE_LIMITED
    assert error.retry_after == 30


def test_a_quota_refusal_may_give_a_date():
    clock = FakeClock(1_785_062_400)  # Sun, 26 Jul 2026 10:40:00 GMT
    env = make(clock=clock, script=[json_reply({}, status=429, headers={
        "Retry-After": "Sun, 26 Jul 2026 10:42:00 GMT",
    })])
    assert failure(env).retry_after == 120


def test_a_quota_refusal_without_a_usable_wait_has_none():
    env = make(script=[json_reply({}, status=429, headers={"Retry-After": "soon"})])
    error = failure(env)
    assert error.code == ErrorCode.RATE_LIMITED and error.retry_after is None


@pytest.mark.parametrize("value, expected", [
    ("0", 0), ("17", 17), (b"17", 17), ("999999999", 86400), ("", None), (None, None),
    ("-5", None), ("1.5", None), ("Sun, 26 Jul 2026 10:39:00 GMT", 0),
])
def test_retry_after_parsing(value, expected):
    assert parse_retry_after(value, now=1_785_062_400) == expected


@pytest.mark.parametrize("body", [
    b"not json",
    b"\xff\xfe",
    b"[]",
    b'{"no_data": true}',
    b'{"data": {"membership_status": "member"}}',
    json.dumps({"data": membership_data(status="maybe")}).encode(),
    json.dumps({"data": membership_data(pubkey=PUBKEY.upper())}).encode(),
    b"[" * 100_000,
])
def test_a_malformed_answer_is_bad_response_not_an_exception(body):
    env = make(script=[FakeReply(status=200, body=body)])
    error = failure(env, "me")
    assert error.code == ErrorCode.BAD_RESPONSE
    assert error.status == 200


def test_an_error_body_that_is_not_json_still_maps_by_status():
    env = make(script=[FakeReply(status=502, body=b"<html>Bad Gateway</html>")])
    error = failure(env)
    assert error.code == ErrorCode.SERVER and error.message == ""


def test_an_oversized_answer_is_aborted_mid_transfer():
    env = make()
    ok, failed = [], []
    env.api.me(ok.append, failed.append)
    reply = env.nam.issued[0]
    reply.downloadProgress.emit(MAX_RESPONSE_BYTES + 1, -1)
    assert reply.aborted
    reply.finish()
    assert ok == [] and failed[0].code == ErrorCode.BAD_RESPONSE


def test_an_announced_oversized_answer_is_aborted_before_it_arrives():
    env = make()
    failed = []
    env.api.me(lambda _v: None, failed.append)
    reply = env.nam.issued[0]
    reply.downloadProgress.emit(10, MAX_RESPONSE_BYTES * 4)
    assert reply.aborted


def test_an_oversized_body_is_refused_even_without_progress():
    big = json.dumps({"data": membership_data(), "pad": "x" * MAX_RESPONSE_BYTES}).encode()
    env = make(script=[FakeReply(status=200, body=big)])
    assert failure(env).code == ErrorCode.BAD_RESPONSE


@pytest.mark.parametrize("error, code", [
    (QNetworkReply.NetworkError.OperationCanceledError, ErrorCode.TIMEOUT),
    (QNetworkReply.NetworkError.TimeoutError, ErrorCode.TIMEOUT),
    (QNetworkReply.NetworkError.HostNotFoundError, ErrorCode.OFFLINE),
    (QNetworkReply.NetworkError.ConnectionRefusedError, ErrorCode.OFFLINE),
    (QNetworkReply.NetworkError.TemporaryNetworkFailureError, ErrorCode.OFFLINE),
    (QNetworkReply.NetworkError.SslHandshakeFailedError, ErrorCode.OFFLINE),
    (QNetworkReply.NetworkError.InsecureRedirectError, ErrorCode.BAD_RESPONSE),
    (QNetworkReply.NetworkError.TooManyRedirectsError, ErrorCode.BAD_RESPONSE),
])
def test_transport_failures_map_to_stable_codes(error, code):
    env = make(script=[transport_failure(error)])
    result = failure(env)
    assert result.code == code and result.status is None


def test_a_redirect_that_was_not_followed_is_bad_response():
    env = make(script=[FakeReply(status=302)])
    assert failure(env).code == ErrorCode.BAD_RESPONSE


def test_a_success_callback_that_raises_does_not_reach_qt(capsys):
    env = make()

    def explode(_value):
        raise RuntimeError("caller bug")

    failed = []
    env.api.me(explode, failed.append)
    env.nam.settle()  # would raise here if the exception escaped
    assert failed == []
    assert "caller bug" in capsys.readouterr().err


# --------------------------------------------------------------------- #
# A slow signer and the one re-sign                                     #
# --------------------------------------------------------------------- #

def test_a_credential_that_expired_on_the_phone_is_signed_once_more():
    # 61 seconds to approve: the event is outside the server's window on
    # arrival. The second approval is quick and goes through.
    clock = FakeClock()
    env = make(clock=clock, signer=FakeSigner(clock=clock, takes=[61, 0]))
    ok, failed = run(env, "me")
    assert failed == [] and isinstance(ok[0], MembershipStatus)
    assert len(env.signer.requests) == 2
    assert env.server.refusals == ["time window"]
    first, second = sent_event(env, 0), sent_event(env, 1)
    assert first["id"] != second["id"]
    assert second["created_at"] > first["created_at"]


def test_the_re_sign_happens_once_only():
    clock = FakeClock()
    env = make(clock=clock, signer=FakeSigner(clock=clock, takes=[61, 61, 61]))
    error = failure(env, "me")
    assert error.code == ErrorCode.UNAUTHORIZED
    assert len(env.signer.requests) == 2


def test_a_prompt_refusal_is_not_re_signed():
    # A fresh credential refused anyway means the key or the signature
    # is wrong; another prompt on the phone would not change that.
    env = make(script=[json_reply({"message": "Unauthenticated."}, status=401)], server=False)
    error = failure(env, "me")
    assert error.code == ErrorCode.UNAUTHORIZED
    assert len(env.signer.requests) == 1


def test_a_slow_signature_is_only_re_signed_for_a_401():
    clock = FakeClock()
    env = make(clock=clock, signer=FakeSigner(clock=clock, takes=[50]),
               script=[json_reply({}, status=500)])
    assert failure(env, "me").code == ErrorCode.SERVER
    assert len(env.signer.requests) == 1


def test_a_re_signed_post_carries_a_fresh_credential_for_the_same_body():
    clock = FakeClock()
    env = make(clock=clock, signer=FakeSigner(clock=clock, takes=[61, 0]))
    ok, failed = run(env, "apply", email="satoshi@example.org")
    assert failed == []
    assert sent_body(env, 0) == sent_body(env, 1)
    assert tag(sent_event(env, 0), "payload") == tag(sent_event(env, 1), "payload")


# --------------------------------------------------------------------- #
# The signer                                                            #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("reason, code", [
    ("timed out waiting for signer", ErrorCode.SIGNER_UNREACHABLE),
    ("could not deliver request to any relay (wss://x: refused)", ErrorCode.SIGNER_UNREACHABLE),
    ("not connected", ErrorCode.SIGNER_UNREACHABLE),
    ("user rejected the request", ErrorCode.SIGNER_DECLINED),
    ("permission denied", ErrorCode.SIGNER_DECLINED),
    ("signer returned an event with an invalid signature", ErrorCode.SIGNER_DECLINED),
    ("", ErrorCode.SIGNER_DECLINED),
])
def test_signer_failures_split_into_declined_and_unreachable(reason, code):
    env = make(signer=FakeSigner(failure=reason))
    error = failure(env, "me")
    assert error.code == code
    assert env.nam.calls == []


def test_a_signer_that_alters_the_request_is_not_sent():
    def move_target(event):
        event["tags"][0] = ["u", "https://elsewhere.example/"]
        return event

    env = make(signer=FakeSigner(tamper=move_target))
    error = failure(env, "me")
    assert error.code == ErrorCode.SIGNER_DECLINED
    assert env.nam.calls == []


def test_a_signer_that_mutates_its_input_is_still_checked_against_the_original():
    def sign_and_scribble(unsigned, on_success, on_failure):
        signer = FakeSigner()
        unsigned["tags"].append(["extra", "x"])
        signer(unsigned, on_success, on_failure)

    env = make(signer=sign_and_scribble)
    assert failure(env, "me").code == ErrorCode.SIGNER_DECLINED


def test_a_signer_adapter_that_raises_does_not_reach_qt():
    def broken(_unsigned, _ok, _fail):
        raise RuntimeError("pool exploded")

    env = make(signer=broken)
    error = failure(env, "me")
    assert error.code == ErrorCode.SIGNER_UNREACHABLE
    assert env.nam.calls == []


def test_a_signer_that_answers_twice_produces_one_outcome():
    def twice(unsigned, on_success, on_failure):
        FakeSigner()(unsigned, on_success, on_failure)
        on_failure("late refusal")
        FakeSigner()(unsigned, on_success, on_failure)

    env = make(signer=twice)
    ok, failed = run(env, "me")
    assert len(ok) == 1 and failed == []
    assert len(env.nam.calls) == 1


def test_cancel_drops_a_call_waiting_on_the_signer():
    env = make(signer=FakeSigner(defer=True))
    ok, failed = [], []
    env.api.me(ok.append, failed.append)
    env.api.cancel()
    env.signer.release()
    env.nam.settle()
    assert ok == [] and failed == [] and env.nam.calls == []


def test_cancel_aborts_a_request_in_flight():
    env = make()
    ok, failed = [], []
    env.api.me(ok.append, failed.append)
    env.api.cancel()
    assert env.nam.issued[0].aborted
    env.nam.settle()
    assert ok == [] and failed == []


def test_calls_after_cancel_work_normally():
    env = make()
    env.api.cancel()
    ok, failed = run(env, "me")
    assert failed == [] and len(ok) == 1


def test_session_signer_adapts_the_bunker_pool():
    seen = {}

    class Client:
        def sign_event(self, unsigned, on_success, on_failure, *, timeout_ms):
            seen["timeout_ms"] = timeout_ms
            FakeSigner()(unsigned, on_success, on_failure)

    class Pool:
        def get(self, profile, on_ready, on_error):
            seen["profile"] = profile
            on_ready(Client())

    env = make(signer=session_signer(Pool(), "profile-x"))
    ok, failed = run(env, "me")
    assert failed == [] and len(ok) == 1
    assert seen == {"profile": "profile-x", "timeout_ms": SIGN_TIMEOUT_MS}


def test_session_signer_reports_a_pool_failure_as_a_signer_failure():
    class Pool:
        def get(self, profile, on_ready, on_error):
            on_error("timed out waiting for signer")

    env = make(signer=session_signer(Pool(), "p"))
    assert failure(env, "me").code == ErrorCode.SIGNER_UNREACHABLE


# --------------------------------------------------------------------- #
# Server words                                                          #
# --------------------------------------------------------------------- #
# (That the association's key never leaks is the service's promise now:
# see tests/test_sidecar.py. The app never holds it.)

def test_a_message_is_cleaned_and_capped():
    env = make(script=[json_reply({"message": "line\none\x00\t" + "x" * 1000}, status=500)])
    message = failure(env).message
    assert "\n" not in message and "\x00" not in message
    assert message.startswith("line one ")
    assert len(message) <= 300


# --------------------------------------------------------------------- #
# Parsing                                                               #
# --------------------------------------------------------------------- #

def test_a_membership_answer_is_read_by_membership_status():
    status = parse_membership(membership_data("lapsed", association_status="ACTIVE"))
    # The category says ACTIVE, the fee is unpaid: not a member.
    assert status.association_status == "ACTIVE"
    assert status.membership_status == "lapsed"
    assert not status.is_member and status.needs_payment
    assert parse_membership(membership_data("member", paid=True)).is_member
    assert parse_membership(membership_data("none")).needs_application


def test_nullable_fields_may_be_null_or_absent():
    data = membership_data()
    data["statutes_accepted_at"] = None
    del data["applied_at"]
    status = parse_membership(data)
    assert status.statutes_accepted_at is None and status.applied_at is None


@pytest.mark.parametrize("edit", [
    lambda d: d.update(fee="21"),
    lambda d: d.update(fee=True),
    lambda d: d.update(year=None),
    lambda d: d.update(currency=""),
    lambda d: d.pop("statutes"),
    lambda d: d["statutes"].update(url="javascript:alert(1)"),
    lambda d: d["statutes"].update(url="file:///etc/passwd"),
    lambda d: d["application"].update(required_fields="statutes_accepted"),
    lambda d: d["application"].pop("application_text_max_length"),
])
def test_a_malformed_config_is_refused(edit):
    data = config_data()
    data["statutes"] = dict(data["statutes"])
    data["application"] = dict(data["application"])
    edit(data)
    with pytest.raises(ValueError):
        parse_config(data)


def test_unknown_extra_fields_are_ignored():
    data = config_data(new_field={"x": 1})
    assert parse_config(data).fee == 21


def test_an_invoice_with_a_payment_request_reports_its_amount():
    bolt11 = (
        "lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypqdq5xysxxatsyp3k7"
        "enxv4jsxqzpuaztrnwngzn3kdzw5hydlzf03qdgm2hdq27cqv3agm2awhz5se903vruatfhq77w3ls4evs3ch9zw9"
        "7j25emudupq63nyw24cg27h2rspfj9srp"
    )
    invoice = parse_invoice(invoice_data(bolt11=bolt11))
    assert invoice.amount_sats == 250_000
    assert not invoice.expired


def test_an_invoice_without_a_checkout_on_refresh_is_expired():
    invoice = parse_invoice(invoice_data(checkout_url=None, created=False))
    assert invoice.expired and invoice.bolt11 is None
    assert invoice.amount_sats is None


def test_a_paid_invoice_is_never_expired():
    assert not parse_invoice(invoice_data(paid=True, checkout_url=None)).expired


@pytest.mark.parametrize("edit", [
    lambda d: d.update(checkout_url="javascript:void(0)"),
    lambda d: d.update(created="yes"),
    lambda d: d.update(bolt11=12),
    lambda d: d.update(payment=None),
    lambda d: d["payment"].update(paid="true"),
    lambda d: d["payment"].update(receipt_url="ftp://x.example/r"),
])
def test_a_malformed_invoice_is_refused(edit):
    data = invoice_data()
    data["payment"] = dict(data["payment"])
    edit(data)
    with pytest.raises(ValueError):
        parse_invoice(data)


def test_payments_parse_in_order():
    entries = parse_payments([
        {"year": 2026, "amount": 21, "currency": "CHF", "paid": True,
         "receipt_url": "https://pay.example/r"},
        {"year": 2025, "amount": 21, "currency": "CHF", "paid": False, "receipt_url": None},
    ])
    assert [e.year for e in entries] == [2026, 2025]
    assert isinstance(entries[0], FeeEntry)
    assert parse_payments([]) == ()
    with pytest.raises(ValueError):
        parse_payments({"year": 2026})


def test_erasure_answers():
    assert parse_erasure({"erased": True, "retained_payments": 2}) == Erasure(True, 2)
    assert parse_erasure({"erased": True, "retained_payments": None}) == Erasure(True, None)
    for bad in ({"erased": True, "retained_payments": -1},
                {"erased": "yes", "retained_payments": 0},
                {"erased": True, "retained_payments": True}):
        with pytest.raises(ValueError):
            parse_erasure(bad)


def test_the_export_keeps_the_whole_document():
    document = {
        "subject": {"pubkey": PUBKEY, "npub": "npub1x"},
        "membership_status": "member",
        "member": {"email": "satoshi@example.org"},
        "payments": [{"year": 2026}],
        "membership_grants": [],
        "nostr_profile": None,
    }
    export = parse_export(document)
    assert export.pubkey == PUBKEY and export.membership_status == "member"
    assert export.document == document
    document["member"]["email"] = "changed"
    assert export.document["member"]["email"] == "satoshi@example.org"
    with pytest.raises(ValueError):
        parse_export({**document, "payments": "none"})
    with pytest.raises(ValueError):
        parse_export({**document, "subject": {"pubkey": "x"}})
