# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the words and the small pure helpers around joining.

The person reading these alerts is joining a club, not debugging a
client, so the copy may name what happened and what to do next, and
nothing else: no protocol names, no status numbers, no "API key". The
house style also bans the em-dash outright, in copy and in source.

The name check and the Lightning amount are pure functions the form
leans on before anything is signed, so they are pinned here too.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from nostr.bech32 import bech32_encode
from nostr.einundzwanzig_api import (
    ERROR_CODES,
    ApiError,
    ErrorCode,
    bolt11_amount_sats,
    email_problem,
    field_message,
    handle_field_message,
    humanize,
    nip05_handle_problem,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Escaped rather than literal so the banned character appears nowhere in
# the tree, including in the test that forbids it.
EM_DASH = "\u2014"

JARGON = re.compile(
    r"nip-?98|nip-?46|nip-?05|bolt-?11|\bkind\b|\b[45]\d\d\b|api|key|http|json|"
    r"nostr|pubkey|npub|signature|header|token|null|bech32|lnurl|webhook|btcpay",
    re.IGNORECASE,
)


def every_error():
    for code in sorted(ERROR_CODES):
        yield ApiError(code)
    yield ApiError(ErrorCode.VALIDATION, status=422, field_errors={"email": ["x"]})
    for wait in (None, 0, 1, 30, 59, 60, 61, 3599, 3600, 7200, 86400):
        yield ApiError(ErrorCode.RATE_LIMITED, status=429, retry_after=wait)
    yield ApiError(ErrorCode.UNAUTHORIZED, status=401, message="Unauthenticated.")
    yield ApiError("no_such_code")


# --------------------------------------------------------------------- #
# Alerts                                                                #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("error", list(every_error()), ids=repr)
def test_every_error_has_a_short_title_and_a_next_step(error):
    title, message = humanize(error)
    assert title and message
    assert len(title) <= 50
    assert not title.endswith(".")
    assert message.endswith(".")


@pytest.mark.parametrize("error", list(every_error()), ids=repr)
def test_the_copy_has_no_jargon(error):
    for text in humanize(error):
        assert not JARGON.search(text), text


@pytest.mark.parametrize("error", list(every_error()), ids=repr)
def test_the_copy_never_uses_we_or_an_em_dash(error):
    for text in humanize(error):
        assert EM_DASH not in text
        assert not re.search(r"\bwe\b|\bour\b", text, re.IGNORECASE)


def test_the_server_message_is_never_shown():
    error = ApiError(ErrorCode.SERVER, status=500, message="SQLSTATE[HY000] connection refused")
    assert "SQLSTATE" not in " ".join(humanize(error))


def test_each_code_has_its_own_title():
    titles = [humanize(ApiError(code))[0] for code in sorted(ERROR_CODES)]
    assert len(set(titles)) == len(titles)


def test_an_unknown_code_falls_back_to_something_honest():
    assert humanize(ApiError("no_such_code")) == humanize(ApiError(ErrorCode.BAD_RESPONSE))


@pytest.mark.parametrize("seconds, phrase", [
    (None, "a few minutes"),
    (1, "1 second,"),
    (30, "30 seconds"),
    (60, "about 1 minute,"),
    (61, "about 2 minutes"),
    (3600, "about 1 hour."),
    (5400, "about 2 hours"),
    (86400, "tomorrow"),
])
def test_a_quota_says_how_long_to_wait(seconds, phrase):
    title, message = humanize(ApiError(ErrorCode.RATE_LIMITED, retry_after=seconds))
    assert title == "Too many attempts"
    assert phrase in message


def test_a_silent_signer_points_at_the_signer_app():
    title, message = humanize(ApiError(ErrorCode.SIGNER_UNREACHABLE))
    assert "signer" in title.lower() and "signer app" in message


def test_unavailable_joining_points_at_the_website():
    assert humanize(ApiError(ErrorCode.UNAVAILABLE)) == (
        "Joining in the app isn't available right now",
        "You can join on the EINUNDZWANZIG website instead.",
    )


def test_a_refused_credential_suggests_the_clock():
    # The likeliest cause a user can fix is a computer clock outside the
    # server's one-minute window.
    assert "time" in humanize(ApiError(ErrorCode.UNAUTHORIZED))[1]


# --------------------------------------------------------------------- #
# Field messages                                                        #
# --------------------------------------------------------------------- #

def handle_error(*reasons):
    return ApiError(ErrorCode.VALIDATION, status=422, field_errors={"nip05_handle": list(reasons)})


@pytest.mark.parametrize("reason", [
    "The nip05 handle has already been taken.",
    "nip05 handle ist bereits vergeben.",
])
def test_a_taken_name_says_so(reason):
    assert handle_field_message(handle_error(reason)) == "That name is taken. Try another one."


@pytest.mark.parametrize("reason", [
    "The nip05 handle field format is invalid.",
    "Das Format von nip05 handle ist ungültig.",
])
def test_an_invalid_name_says_which_characters_work(reason):
    message = handle_field_message(handle_error(reason))
    assert "lowercase letters" in message and "underscores" in message


def test_a_too_long_name_says_the_limit():
    message = handle_field_message(
        handle_error("The nip05 handle field must not be greater than 255 characters.")
    )
    assert "255" in message


def test_an_unrecognised_name_refusal_still_reads_plainly():
    assert handle_field_message(handle_error("Computer says no.")) == (
        "This name cannot be used. Try another one."
    )


def test_no_name_error_means_no_message():
    assert handle_field_message(ApiError(ErrorCode.VALIDATION)) is None
    assert handle_field_message(ApiError(ErrorCode.SERVER)) is None
    assert field_message(ApiError(ErrorCode.VALIDATION, field_errors={"email": ["x"]}),
                         "nip05_handle") is None


@pytest.mark.parametrize("field", ["email", "application_text", "statutes_accepted", "other"])
def test_every_field_gets_plain_inline_words(field):
    error = ApiError(ErrorCode.VALIDATION, field_errors={field: ["validation.required"]})
    message = field_message(error, field)
    assert message and message.endswith(".")
    assert "validation." not in message
    assert not JARGON.search(message) and EM_DASH not in message


def test_the_generic_field_helper_covers_the_name():
    error = handle_error("The nip05 handle has already been taken.")
    assert field_message(error, "nip05_handle") == handle_field_message(error)


# --------------------------------------------------------------------- #
# Checking the name before it is sent                                   #
# --------------------------------------------------------------------- #

@pytest.mark.parametrize("handle", ["satoshi", "s", "hal-finney", "nick_szabo", "21", "a" * 255])
def test_names_the_server_accepts_pass(handle):
    assert nip05_handle_problem(handle) is None


@pytest.mark.parametrize("handle, words", [
    ("", "Enter a name"),
    ("Satoshi", "lowercase letters only"),
    ("satoshi nakamoto", "Spaces are not allowed"),
    ("satoshi@einundzwanzig.space", "before the @"),
    ("sätoshi", "Use only lowercase letters"),
    ("satoshi.n", "Use only lowercase letters"),
    ("a" * 256, "255 characters or fewer"),
])
def test_names_the_server_refuses_get_a_reason(handle, words):
    problem = nip05_handle_problem(handle)
    assert problem and words in problem
    assert not JARGON.search(problem) and EM_DASH not in problem


def test_the_check_matches_the_server_rule():
    # ^[a-z0-9_-]+$, max 255, straight from the spec.
    rule = re.compile(r"[a-z0-9_-]+")
    for candidate in ["abc", "ABC", "a-b", "a_b", "a.b", "a b", "a\nb", "ä", "-", "_", "0"]:
        assert (nip05_handle_problem(candidate) is None) == bool(rule.fullmatch(candidate))
    # "$" in the spec's pattern also matches before a trailing newline;
    # a name ending in one is still not a name.
    assert nip05_handle_problem("abc\n") is not None


@pytest.mark.parametrize("address, ok", [
    ("satoshi@example.org", True),
    ("a@b", True),
    ("satoshi", False),
    ("sat oshi@example.org", False),
    ("@example.org", False),
    ("a" * 250 + "@x.org", False),
])
def test_email_check(address, ok):
    assert (email_problem(address) is None) is ok


# --------------------------------------------------------------------- #
# Lightning amounts                                                     #
# --------------------------------------------------------------------- #

# The coffee example from the BOLT 11 specification: 2500u is 250,000 sat.
SPEC_INVOICE = (
    "lnbc2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypqdq5xysxxatsyp3k7"
    "enxv4jsxqzpuaztrnwngzn3kdzw5hydlzf03qdgm2hdq27cqv3agm2awhz5se903vruatfhq77w3ls4evs3ch9zw9"
    "7j25emudupq63nyw24cg27h2rspfj9srp"
)


def invoice(hrp: str) -> str:
    """A checksummed bech32 string with ``hrp``, standing in for an invoice."""
    return bech32_encode(hrp, list(range(32)) * 3)


def test_the_specification_example():
    assert bolt11_amount_sats(SPEC_INVOICE) == 250_000


@pytest.mark.parametrize("hrp, sats", [
    ("lnbc1", 100_000_000),          # one bitcoin, no multiplier
    ("lnbc21m", 2_100_000),          # milli
    ("lnbc210u", 21_000),            # micro: the spec's own example amount
    ("lnbc2500u", 250_000),
    ("lnbc10n", 1),                  # nano: 0.1 sat each
    ("lnbc1n", 1),                   # 0.1 sat rounds UP, never shows less
    ("lnbc15n", 2),
    ("lnbc10000p", 1),               # pico: 10 p is one millisatoshi
    ("lnbc12340p", 2),               # 1.234 sat rounds up
    ("lntb21m", 2_100_000),          # testnet
    ("lnbcrt5u", 500),               # regtest
    ("lntbs1u", 100),                # signet
])
def test_amounts_by_multiplier(hrp, sats):
    assert bolt11_amount_sats(invoice(hrp)) == sats


@pytest.mark.parametrize("value", [
    invoice("lnbc"),                       # no amount: the payer chooses
    invoice("lnbc021u"),                   # leading zero
    invoice("lnbc0u"),
    invoice("lnbc11p"),                    # pico not a whole millisatoshi
    invoice("lnbc21x"),                    # unknown multiplier
    invoice("lnxy21u"),                    # unknown network
    invoice("npub"),                       # not an invoice at all
    SPEC_INVOICE[:-1] + "q",               # checksum broken
    "lnbc210u1p48naztpp5tl2nrhjn8skyy3jv0mlhfaugppmu6dv",  # the spec's truncated example
    "",
    "not an invoice",
    None,
    12345,
])
def test_no_amount_or_no_invoice_is_none(value):
    assert bolt11_amount_sats(value) is None


def test_upper_case_and_a_lightning_prefix_are_read():
    # QR codes carry invoices upper-cased, and wallets add the scheme.
    assert bolt11_amount_sats(SPEC_INVOICE.upper()) == 250_000
    assert bolt11_amount_sats("lightning:" + SPEC_INVOICE) == 250_000
    assert bolt11_amount_sats("LIGHTNING:" + SPEC_INVOICE.upper()) == 250_000
    assert bolt11_amount_sats("  " + SPEC_INVOICE + "\n") == 250_000


# --------------------------------------------------------------------- #
# House style                                                           #
# --------------------------------------------------------------------- #

def _files_in(folder):
    path = os.path.join(ROOT, folder)
    return sorted(f"{folder}/{name}" for name in os.listdir(path)
                  if os.path.isfile(os.path.join(path, name))
                  and name != ".env" and not name.endswith(".pyc"))


@pytest.mark.parametrize("path", [
    "nostr/nip98.py",
    *_files_in("nostr/einundzwanzig_api"),
    "tests/membership_fakes.py",
    "tests/test_nip98.py",
    "tests/test_einundzwanzig_api.py",
    "tests/test_einundzwanzig_payment_watcher.py",
    "tests/test_einundzwanzig_copy.py",
    "tests/test_sidecar.py",
    "tests/smoke_sidecar_e2e.py",
    "constants.py",
    "packaging/my_editor.spec",
    ".github/workflows/build-installers.yml",
    ".gitignore",
    *_files_in("sidecar"),
])
def test_no_em_dash_in_the_files_this_feature_touches(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        assert EM_DASH not in handle.read()
