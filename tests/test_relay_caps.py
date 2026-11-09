# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a signer connection's relays carry bounds every request (P1;
STANDUP's editorial contract 12.5 "Relay content caps", feat/redaktion
4076b9b): nostr/signer_limits.py, the one place the bound is taken from,
the NIP-11 reader that feeds it (nostr/relay_caps.py), the session pool
that waits for it, and every caller that asks it (the signer client, the
drafts, the private lists, a STANDUP submission's message)."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QApplication

from nostr import answer_size, bunker, crypto, relay_caps, signer_limits
from nostr.paged_list import Paging, split
from nostr.signer_limits import (
    NIP44_MESSAGE_MAX, SIGNER_ANSWER_MAX, SIGNER_NIP44_MAX_PLAINTEXT, connection_answer_limit,
    connection_message_limit, connection_plaintext_limit, note_relay_cap,
)
from tests.blossom_fakes import FakeNam, FakeReply, json_reply, redirect_reply
from tests.test_bunker_request_size import EncryptingSigner

PK = "ab" * 32
NIP46 = "wss://relay.nip46.com"            # the relay whose cap the contract names
CAPPED = SimpleNamespace(user_pubkey=PK, signer="remote", bunker_relays=[NIP46])
MIXED = SimpleNamespace(user_pubkey=PK, signer="remote", bunker_relays=[NIP46, "wss://nos.lol"])
OPEN = SimpleNamespace(user_pubkey=PK, signer="remote", bunker_relays=["wss://nos.lol"])
LOCAL = SimpleNamespace(user_pubkey=PK, signer="local", bunker_relays=[NIP46])
CONNECTION_SIGN = ("The connection to your signer app carries only short messages. "
                   "Very long texts can’t be signed through it.")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def nothing_read():
    """Every test starts and ends with no relay's document read."""
    signer_limits.forget_relay_caps()
    yield
    signer_limits.forget_relay_caps()


def _turn() -> None:
    QCoreApplication.processEvents()


# -- the measure (contract 12.5) ----------------------------------------------------

def test_a_65536_character_cap_carries_40960_bytes_as_the_contract_says():
    assert signer_limits.message_bytes_for_content_cap(65_536) == 40_960
    assert crypto.payload_length(40_960) == 54_704
    # The next padding step would need 65,628 characters.
    assert crypto.payload_length(40_961) == 65_628
    assert signer_limits.message_bytes_for_content_cap(65_628) == 49_152
    assert signer_limits.message_bytes_for_content_cap(10) == 0
    assert signer_limits.message_bytes_for_content_cap(10**9) == NIP44_MESSAGE_MAX


@pytest.mark.parametrize("limitation, cap", [
    ({"max_content_length": 65_536}, 65_536),
    ({"max_message_length": 131_072}, 131_072 - 512),
    ({"max_content_length": 65_536, "max_message_length": 60_000}, 60_000 - 512),
    ({"max_content_length": True}, None),
    ({"max_content_length": -1, "max_message_length": "65536"}, None),
    ({}, None),
])
def test_the_cap_a_document_states(limitation, cap):
    assert signer_limits.content_cap_of({"limitation": limitation}) == cap


@pytest.mark.parametrize("document", [None, [], "x", {"limitation": [1]}, {"name": "r"}])
def test_a_document_without_a_limitation_states_no_cap(document):
    assert signer_limits.content_cap_of(document) is None


@pytest.mark.parametrize("url, key", [
    ("wss://Relay.Example.com/", "wss://relay.example.com"),
    ("WSS://relay.example.com/path/", "wss://relay.example.com/path"),
    ("ws://relay.example.com:7777", "ws://relay.example.com:7777"),
    ("https://relay.example.com", None),
    ("wss://user@relay.example.com", None),
    ("wss://relay.example.com:99999", None),
    ("relay.example.com", None),
])
def test_a_relay_is_known_in_one_spelling(url, key):
    assert signer_limits.relay_key(url) == key


def test_the_largest_cap_applies_and_only_when_every_relay_states_one():
    assert connection_message_limit([]) == NIP44_MESSAGE_MAX
    # Unread: no cap, except the relay the contract names.
    assert connection_message_limit(["wss://nos.lol"]) == NIP44_MESSAGE_MAX
    assert connection_message_limit([NIP46]) == 40_960
    assert connection_message_limit([NIP46, "wss://nos.lol"]) == NIP44_MESSAGE_MAX
    note_relay_cap("wss://small.example", 20_000)
    assert connection_message_limit(["wss://small.example"]) == \
        signer_limits.message_bytes_for_content_cap(20_000) < 40_960
    assert connection_message_limit(["wss://small.example", NIP46]) == 40_960
    note_relay_cap("wss://nos.lol", None)                   # read: no cap
    assert connection_message_limit([NIP46, "wss://nos.lol"]) == NIP44_MESSAGE_MAX
    note_relay_cap(NIP46, 131_072)                          # read: a larger cap than named
    assert connection_message_limit([NIP46]) == NIP44_MESSAGE_MAX
    note_relay_cap(NIP46, None, read=False)                 # could not be read: as named
    assert connection_message_limit([NIP46]) == 40_960


def test_answers_and_plaintexts_follow_from_the_message_limit():
    assert connection_answer_limit([NIP46]) == 40_960 - 64
    assert connection_answer_limit(["wss://nos.lol"]) == SIGNER_ANSWER_MAX
    # The contract: list pages of 28,672 bytes of plaintext for 40,960.
    assert connection_plaintext_limit([NIP46]) == 28_672
    assert connection_plaintext_limit(["wss://nos.lol"]) == SIGNER_NIP44_MAX_PLAINTEXT


def test_a_payload_whose_decryption_request_fits_has_an_encryption_answer_that_fits():
    # Why connection_plaintext_limit measures the request alone.
    assert signer_limits._DECRYPT_REQUEST >= signer_limits._ENCRYPT_ANSWER + \
        signer_limits.ANSWER_EXTRA
    for cap in (432, 2_000, 65_536, 90_000):
        note_relay_cap("wss://capped.example", cap)
        relays = ["wss://capped.example"]
        limit = connection_plaintext_limit(relays)
        payload = crypto.payload_length(max(1, limit))
        if limit:
            assert signer_limits._ENCRYPT_ANSWER + payload <= connection_answer_limit(relays)
            assert signer_limits._DECRYPT_REQUEST + payload <= \
                connection_message_limit(relays)
    note_relay_cap("wss://capped.example", 432)
    assert connection_plaintext_limit(["wss://capped.example"]) == 0


def test_the_documents_kept_are_bounded():
    for index in range(signer_limits._KEPT_CAPS + 5):
        note_relay_cap(f"wss://r{index}.example", 1_000)
    assert len(signer_limits._read_caps) == signer_limits._KEPT_CAPS
    assert not signer_limits.relay_read("wss://r0.example")
    assert signer_limits.relay_read(f"wss://r{signer_limits._KEPT_CAPS + 4}.example")


# -- one account's signer -------------------------------------------------------------

def test_an_accounts_bounds_are_its_connections():
    assert signer_limits.nip44_plaintext_limit(CAPPED) == 28_672
    assert signer_limits.nip44_plaintext_limit(MIXED) == SIGNER_NIP44_MAX_PLAINTEXT
    assert signer_limits.nip44_plaintext_limit(LOCAL) == crypto.NIP44_MAX_PLAINTEXT_LEN
    assert signer_limits.capped(CAPPED) and not signer_limits.capped(OPEN)
    assert not signer_limits.capped(LOCAL)


def test_a_draft_the_capped_connection_cannot_hand_back_is_not_readable_back():
    text = "x" * 30_000
    assert signer_limits.readable_back(OPEN, text)
    assert not signer_limits.readable_back(CAPPED, text)
    assert signer_limits.readable_back(LOCAL, text)
    assert signer_limits.readable_back(CAPPED, "x" * 28_672)


def test_the_request_that_encrypts_a_draft_must_fit_too():
    # 32,705 quotes: the answer handing them back fits (65,460 bytes), the
    # request carrying them, each quote escaped, does not (65,540).
    text = '"' * 32_705
    assert answer_size.decrypt_answer_size(text) <= SIGNER_ANSWER_MAX
    assert answer_size.encrypt_request_size(text) > NIP44_MESSAGE_MAX
    assert not signer_limits.readable_back(OPEN, text)
    assert signer_limits.readable_back(OPEN, '"' * 32_690)


def test_the_request_is_measured_as_the_client_sends_it():
    text = 'ä"<\n\b'
    sent = json.dumps({"id": "0" * 16, "method": "nip44_encrypt", "params": [PK, text]},
                      separators=(",", ":"), ensure_ascii=False)
    assert answer_size.encrypt_request_size(text) == len(sent.encode("utf-8"))


def test_an_event_the_capped_connection_cannot_carry_is_said_plainly():
    event = {"kind": 30023, "content": "x" * 45_000, "tags": [], "created_at": 1}
    assert signer_limits.signable(OPEN, event)
    assert not signer_limits.signable(CAPPED, event)
    assert signer_limits.size_problem(CAPPED, event=event) == CONNECTION_SIGN
    assert signer_limits.size_problem(OPEN, event=event) is None
    longer = dict(event, content="x" * 70_000)
    assert signer_limits.size_problem(CAPPED, event=longer).startswith(
        "This is too long to be signed through your signer app.")


def test_a_draft_the_connection_cannot_carry_is_said_plainly():
    words = signer_limits.size_problem(CAPPED, draft="x" * 30_000)
    assert words.startswith("The connection to your signer app carries only short messages.")
    assert "drafts" in words and "30" not in words          # never the numbers
    assert signer_limits.size_problem(OPEN, draft="x" * 30_000) is None


def test_the_words_are_the_contracts_in_german():
    import i18n
    i18n.install("de")
    try:
        words = signer_limits.size_problem(
            CAPPED, event={"kind": 1, "content": "x" * 45_000, "tags": [], "created_at": 1})
    finally:
        i18n.install("en")
    assert words == ("Die Verbindung zu deiner Signer-App trägt nur kurze Nachrichten. "
                     "Sehr lange Texte lassen sich darüber nicht signieren.")


# -- the signer client refuses before it asks ------------------------------------------

def _pair(relay: str):
    from tests.test_bunker_handshake import FakePool
    from urllib.parse import quote
    pool = FakePool()
    signer = EncryptingSigner(pool)
    client = bunker.BunkerClient(pool)
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    outcome = {}
    client.connect_to_bunker(f"bunker://{signer.pk}?relay={quote(relay, safe='')}&secret=abc",
                             on_success=lambda pk: outcome.setdefault("ok", pk),
                             on_failure=lambda r: outcome.setdefault("err", r),
                             local_sk=local_sk)
    assert outcome.get("ok") == signer.user_pk, outcome
    return client, signer


def _ask(call, *args):
    outcome = {}
    call(*args, on_success=lambda value: outcome.setdefault("ok", value),
         on_failure=lambda reason: outcome.setdefault("err", reason))
    _turn()
    return outcome


def test_the_client_encrypts_no_more_than_the_capped_relays_carry():
    client, signer = _pair(NIP46)
    asked = len(signer.seen)
    refused = _ask(client.nip44_encrypt, PK, "x" * 28_673)
    assert "at most 28672" in refused.get("err", ""), refused
    assert len(signer.seen) == asked                         # never asked
    assert "ok" in _ask(client.nip44_encrypt, PK, "x" * 28_672)


def test_the_client_signs_no_more_than_the_capped_relays_carry():
    client, signer = _pair(NIP46)
    asked = len(signer.seen)
    event = {"kind": 1, "content": "x" * 45_000, "tags": [], "created_at": 1}
    refused = _ask(client.sign_event, event)
    assert "sign_event" in refused.get("err", "") and "40896" in refused["err"], refused
    assert len(signer.seen) == asked


def test_the_client_sends_no_request_longer_than_the_capped_relays_carry():
    client, signer = _pair(NIP46)
    asked = len(signer.seen)
    # A decryption's request carries the payload: 54,704 characters is
    # more than 40,960 bytes, whatever the answer would be.
    payload = crypto.encrypt("x" * 40_000, bytes(range(32)))
    refused = _ask(client.nip44_decrypt, PK, payload)
    assert "nip44_decrypt" in refused.get("err", "") and "40960" in refused["err"], refused
    assert len(signer.seen) == asked


def test_over_an_uncapped_relay_the_same_requests_are_sent():
    client, signer = _pair("wss://fake")
    assert "ok" in _ask(client.nip44_encrypt, PK, "x" * 28_673)


# -- a private list's pages ------------------------------------------------------------

def _pages_of(profile, entries):
    from nostr.encrypted_lists import EncryptedList
    owner = SimpleNamespace(_encode=lambda value, now: {"items": value}, _paging=Paging("items"))
    return EncryptedList._plaintexts(owner, entries, 0, profile)


def test_a_list_is_split_into_pages_the_capped_connection_carries():
    entries = [f'item "{index}" ' + "y" * 80 for index in range(1_500)]
    open_pages = _pages_of(OPEN, entries)
    capped_pages = _pages_of(CAPPED, entries)
    assert len(capped_pages) > len(open_pages)
    for text in capped_pages:
        assert len(text.encode("utf-8")) <= 28_672
        assert answer_size.decrypt_answer_size(text) <= 40_896
        assert answer_size.encrypt_request_size(text) <= 40_960
    assert _pages_of(LOCAL, entries) == open_pages == _pages_of(None, entries)


def test_a_capped_accounts_pages_fit_the_request_that_carries_them():
    # One quote a time: the page fills up to the answer's bound, and the
    # request, 16 bytes tighter, must still hold it.
    for text in _pages_of(CAPPED, ['"'] * 10_000):
        assert answer_size.encrypt_request_size(text) <= 40_960
        assert answer_size.decrypt_answer_size(text) <= 40_896


def test_a_capped_account_writes_its_list_in_pages_its_connection_carries():
    from nostr.list_results import PublishStatus
    from tests.outbox_fakes import PK as OWN, settle
    from tests.test_encrypted_lists import SUBS, Relay, submission, submissions_list
    items = [submission(n) for n in range(200)]
    largest = {}
    for name, relays in (("open", ["wss://nos.lol"]), ("capped", [NIP46])):
        profile = SimpleNamespace(user_pubkey=OWN, signer="remote", bunker_relays=relays)
        relay = Relay()
        results = []
        submissions_list(relay).publish(profile, state=lambda: (None, items, None),
                                        on_done=results.append,
                                        on_merged=lambda *_args: None)
        settle(40)
        assert results and results[-1].status is PublishStatus.SENT
        largest[name] = max(len(relay.plaintext(d).encode("utf-8"))
                            for d in relay.stored if d.startswith(SUBS))
    assert largest["capped"] <= 28_672 < largest["open"]


def test_a_list_is_split_again_once_the_signer_and_its_bound_are_known():
    # Lesson 29: the bound is read as the signer reattaches; a write that
    # started before splits again with it, rather than have the client
    # refuse the pages.
    from nostr.encrypted_lists import EncryptedList
    from nostr.list_results import PublishStatus
    from tests.outbox_fakes import PK as OWN, FakeRelayDirectory, settle
    from tests.test_encrypted_lists import NOW, SUBS, WORDS, Relay, by_sid, submission
    from tests.test_imports_subscriptions import FakeSessionPool, FakeSigner

    class Reattaching(FakeSessionPool):
        def get(self, profile, on_ready, on_error):
            note_relay_cap("wss://late.example", 65_536)      # read as it reattaches
            super().get(profile, on_ready, on_error)

    profile = SimpleNamespace(user_pubkey=OWN, signer="remote",
                              bunker_relays=["wss://late.example"])
    relay = Relay()
    results = []
    store = EncryptedList(
        d_tag=SUBS, decode=lambda payload: by_sid(None, payload["items"], []),
        encode=lambda value, now: {"items": list(value)}, merge=by_sid, words=WORDS,
        session_pool=Reattaching(FakeSigner()), relay_directory=FakeRelayDirectory(),
        query=relay, publisher=relay, clock=lambda: NOW, paging=Paging(field="items"))
    store.publish(profile, state=lambda: (None, [submission(n) for n in range(200)], None),
                  on_done=results.append, on_merged=lambda *_args: None)
    settle(40)
    assert results and results[-1].status is PublishStatus.SENT
    assert max(len(relay.plaintext(d).encode("utf-8"))
               for d in relay.stored if d.startswith(SUBS)) <= 28_672


def test_a_post_is_measured_again_once_the_signer_and_its_bound_are_known():
    from nostr.publisher import PublishJob
    from tests.outbox_fakes import FakeClient, FakePool, FakeRelayDirectory, settle
    from tests.test_signer_limits import CountingPool

    class Reattaching(CountingPool):
        def get(self, profile, on_ready, on_error):
            note_relay_cap("wss://late.example", 65_536)
            super().get(profile, on_ready, on_error)

    client = FakeClient()
    profile = SimpleNamespace(user_pubkey=PK, signer="remote",
                              bunker_relays=["wss://late.example"])
    event = {"kind": 1, "pubkey": PK, "content": "x" * 45_000, "tags": [], "created_at": 1}
    job = PublishJob(relay_pool=FakePool(), relay_directory=FakeRelayDirectory(),
                     session_pool=Reattaching(client), profile=profile, unsigned_event=event)
    failed = []
    job.failed.connect(failed.append)
    job.start()
    settle()
    assert failed == [CONNECTION_SIGN] and client.requests == []


def test_a_page_of_quotes_is_split_by_the_request_that_carries_it():
    # Quotes double in the request: the plaintext and the answer bounds
    # alone would let this page through and the client refuse to send it.
    entries = ['"' * 100] * 160
    paging = Paging("items", max_bytes=28_672, answer_max=40_896, request_max=40_960)
    for text in split({"items": entries}, paging):
        assert answer_size.encrypt_request_size(text) <= 40_960
    loose = Paging("items", max_bytes=28_672, answer_max=10**6)
    assert max(answer_size.encrypt_request_size(text)
               for text in split({"items": entries}, loose)) > 40_960


def test_an_entry_no_capped_page_can_carry_is_refused_with_why():
    paging = Paging("items", max_bytes=28_672, answer_max=10**6, request_max=40_960)
    with pytest.raises(ValueError, match="request to encrypt it"):
        split({"items": ['"' * 14_000]}, paging)     # 28,002 bytes: the page takes it


# -- a STANDUP message, sealed --------------------------------------------------------

def test_a_message_whose_seal_the_connection_cannot_carry_blocks_the_submission():
    from nostr.standup import conversation, drafting
    from tests.standup_vectors import pk
    from tests.test_standup_drafting import DRAFT
    writer = pk("writer")
    message = "ж" * 14_500                       # 29,000 bytes: STANDUP takes it
    assert conversation.message_size(message, sender=writer) <= 32_768

    def blockers(profile):
        return drafting.blockers(DRAFT, writer=writer, rights=True, rights_required=False,
                                 message=message,
                                 signable=lambda unsigned: signer_limits.signable(profile,
                                                                                  unsigned))

    found = blockers(CAPPED)
    assert found == [drafting.MESSAGE_TOO_LONG_FOR_CONNECTION]
    assert "Sealed, it takes" in found[0].detail
    assert blockers(OPEN) == [] and blockers(LOCAL) == []


class HeldSessions:
    def __init__(self):
        self.asked = []

    def get(self, profile, on_ready, on_error):
        self.asked.append((profile, on_ready, on_error))


def _standup_writer(sessions, account):
    from nostr.ui.standup_submit import StandupWriter
    return StandupWriter(session_pool=sessions, relay_pool=None, relay_directory=None,
                         media_store=None, server="https://s.example",
                         uploads=lambda *a: None, accounts=lambda _profile: account)


@pytest.mark.parametrize("answer", ["ready", "error"])
def test_the_writer_learns_the_bound_by_getting_the_signer(answer):
    sessions = HeldSessions()
    account = SimpleNamespace(writer=PK, stopped=False, runs=[], deleteLater=lambda: None)
    told = []
    _standup_writer(sessions, account).signer_bound(CAPPED, lambda: told.append(1))
    (profile, on_ready, on_error), = sessions.asked
    assert profile is CAPPED and told == []
    if answer == "ready":
        on_ready(object())
    else:
        on_error("the signer app did not answer")     # submitting stops at its signature
    assert told == [1]


def test_a_bound_known_already_asks_no_signer():
    note_relay_cap(NIP46, 65_536)
    sessions = HeldSessions()
    account = SimpleNamespace(writer=PK, stopped=False, runs=[], deleteLater=lambda: None)
    told = []
    _standup_writer(sessions, account).signer_bound(CAPPED, lambda: told.append(1))
    assert sessions.asked == [] and told == []
    _turn()
    assert told == [1]


def test_an_account_let_go_hears_no_bound():
    sessions = HeldSessions()
    account = SimpleNamespace(writer=PK, stopped=False, runs=[], deleteLater=lambda: None)
    told = []
    _standup_writer(sessions, account).signer_bound(CAPPED, lambda: told.append(1))
    account.stopped = True
    sessions.asked[0][1](object())
    assert told == []


def test_a_bound_is_known_once_every_relay_was_asked_or_one_carries_all():
    assert signer_limits.bound_known(LOCAL)
    assert signer_limits.bound_known(SimpleNamespace(signer="remote", bunker_relays=[]))
    assert not signer_limits.bound_known(MIXED)
    note_relay_cap(NIP46, None, read=False)                 # asked: unreadable
    assert not signer_limits.bound_known(MIXED)
    note_relay_cap("wss://nos.lol", 65_536)
    assert signer_limits.bound_known(MIXED)
    signer_limits.forget_relay_caps()
    note_relay_cap("wss://nos.lol", None)                   # one carries all
    assert signer_limits.bound_known(MIXED)


def test_the_seal_measure_is_as_large_as_the_seal():
    from nostr.standup import conversation, formats
    room_sk = crypto.generate_secret_key()
    sender_sk = crypto.generate_secret_key()
    rumor = {"kind": 14, "pubkey": crypto.get_public_key(sender_sk).hex(), "content": "ж" * 900,
             "tags": [], "created_at": 1}
    seal = conversation.seal_unsigned(rumor, room_sk, now=2_000_000_000)
    measure = conversation.seal_measure(len(formats.canonical_bytes(rumor)))
    assert len(measure["content"]) == len(seal["content"])
    assert answer_size.sign_answer_size(measure) >= answer_size.sign_answer_size(seal)


# -- reading the documents (nostr/relay_caps.py) ---------------------------------------

def _document(cap=None, **limitation) -> FakeReply:
    if cap is not None:
        limitation["max_content_length"] = cap
    return json_reply({"name": "r", "limitation": limitation})


def test_a_document_is_read_over_https_as_nip11_asks():
    nam = FakeNam([_document(65_536)])
    reader = relay_caps.RelayCapsReader(nam=nam)
    reader.read(["wss://Capped.Example/"])
    (verb, request, _body), = nam.calls
    assert verb == "get" and request.url().toString() == "https://capped.example"
    assert bytes(request.rawHeader("Accept")) == b"application/nostr+json"
    assert request.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute) == \
        QNetworkRequest.RedirectPolicy.ManualRedirectPolicy
    assert request.attribute(QNetworkRequest.Attribute.CookieLoadControlAttribute) == \
        QNetworkRequest.LoadControl.Manual
    assert request.transferTimeout() == relay_caps.NIP11_TIMEOUT_MS
    nam.settle()
    assert connection_message_limit(["wss://capped.example"]) == 40_960


def test_a_ws_relay_is_read_over_http_and_nothing_else_is_read():
    assert relay_caps.document_url("ws://relay.example:7777/x") == "http://relay.example:7777/x"
    assert relay_caps.document_url("https://relay.example") is None
    nam = FakeNam()
    relay_caps.RelayCapsReader(nam=nam).read(["https://relay.example", "nonsense"])
    assert nam.calls == []


@pytest.mark.parametrize("reply", [
    lambda: redirect_reply("https://elsewhere.example/doc"),
    lambda: FakeReply(status=404, error=QNetworkReply.ContentNotFoundError),
    lambda: FakeReply(body=b"not json"),
    lambda: FakeReply(body=b"[1]"),
    lambda: FakeReply(body=b"[" * 60_000),
    lambda: FakeReply(status=302, body=json.dumps(
        {"limitation": {"max_content_length": 1_000}}).encode()),
    lambda: FakeReply(body=json.dumps({"limitation": {"max_content_length": 1_000},
                                       "pad": "x" * relay_caps.NIP11_MAX_BYTES}).encode()),
], ids=["redirect", "missing", "not-json", "not-an-object", "nested", "redirect-with-body",
         "too-long"])
def test_a_document_that_cannot_be_read_counts_as_unread(reply):
    nam = FakeNam([reply()])
    told = []
    relay_caps.RelayCapsReader(nam=nam).read([NIP46], lambda: told.append(1))
    nam.settle()
    _turn()
    assert told == [1]                                      # the reading ended
    assert not signer_limits.relay_read(NIP46)
    assert connection_message_limit([NIP46]) == 40_960       # as the contract names it


def test_a_long_document_stops_being_read():
    nam = FakeNam([_document(1_000)])
    relay_caps.RelayCapsReader(nam=nam).read(["wss://long.example"])
    reply = nam.issued[0]
    reply.progress(relay_caps.NIP11_MAX_BYTES + 1)
    assert reply.aborted


def test_a_relay_that_says_nothing_is_given_up_on(monkeypatch):
    monkeypatch.setattr(relay_caps, "NIP11_TIMEOUT_MS", 1)
    nam = FakeNam()
    told = []
    relay_caps.RelayCapsReader(nam=nam).read(["wss://silent.example"], lambda: told.append(1))
    reply = nam.issued[0]
    for _ in range(50):
        if reply.aborted:
            break
        QCoreApplication.processEvents()
        import time
        time.sleep(0.002)
    assert reply.aborted
    reply.finish()                                  # Qt ends an aborted reply
    _turn()
    assert told == [1] and not signer_limits.relay_read("wss://silent.example")


def test_a_relay_read_once_is_not_read_again():
    nam = FakeNam([_document(None)])
    reader = relay_caps.RelayCapsReader(nam=nam)
    reader.read(["wss://open.example"])
    reader.read(["wss://open.example"])                     # still being read
    nam.settle()
    reader.read(["wss://open.example"])                     # read
    assert len(nam.calls) == 1


def test_the_bound_is_known_once_one_relay_states_no_cap():
    nam = FakeNam([_document(65_536), _document(None)])
    told = []
    relay_caps.RelayCapsReader(nam=nam).read(["wss://a.example", "wss://b.example"],
                                             lambda: told.append(1))
    _turn()
    assert told == []
    nam.issued[1].finish()                          # b.example: no cap
    _turn()
    assert told == [1]                              # a.example still reading


def test_the_bound_is_known_once_every_capped_relay_answered():
    nam = FakeNam([_document(65_536), FakeReply(status=500,
                                                error=QNetworkReply.InternalServerError)])
    told = []
    relay_caps.RelayCapsReader(nam=nam).read(["wss://a.example", "wss://b.example"],
                                             lambda: told.append(1))
    nam.issued[0].finish()
    _turn()
    assert told == []
    nam.issued[1].finish()
    _turn()
    assert told == [1]


def test_a_bound_known_already_is_said_on_a_later_turn():
    note_relay_cap("wss://a.example", 65_536)
    nam = FakeNam()
    told = []
    relay_caps.RelayCapsReader(nam=nam).read(["wss://a.example"], lambda: told.append(1))
    assert told == [] and nam.calls == []
    _turn()
    assert told == [1]


# -- the session pool waits for the bound -----------------------------------------------

class ManualCaps:
    def __init__(self):
        self.asked = []

    def read(self, relays, done):
        self.asked.append((list(relays), done))


def _profile(signer):
    return SimpleNamespace(user_pubkey=signer.user_pk, bunker_pubkey=signer.pk,
                           bunker_relays=[NIP46], local_secret_hex="11" * 32, signer="remote")


def test_a_reattached_signer_is_handed_out_once_its_bound_is_known():
    from tests.test_bunker_handshake import FakePool
    pool = FakePool()
    signer = EncryptingSigner(pool)
    signer.watch(crypto.get_public_key(bytes.fromhex("11" * 32)).hex())
    caps = ManualCaps()
    sessions = bunker.BunkerSessionPool(pool, relay_caps=caps)
    ready, errors = [], []
    sessions.get(_profile(signer), ready.append, errors.append)
    for _ in range(5):
        _turn()
    (relays, done), = caps.asked
    assert relays == [NIP46]
    assert ready == [] and errors == []             # attached, the bound not known yet
    done()
    assert len(ready) == 1 and ready[0].is_connected
    done()                                          # said twice: handed out once
    assert len(ready) == 1 and ready[0].is_connected


def test_without_a_reader_a_reattached_signer_is_handed_out_at_once():
    from tests.test_bunker_handshake import FakePool
    pool = FakePool()
    signer = EncryptingSigner(pool)
    signer.watch(crypto.get_public_key(bytes.fromhex("11" * 32)).hex())
    sessions = bunker.BunkerSessionPool(pool)
    ready = []
    sessions.get(_profile(signer), ready.append, lambda reason: None)
    for _ in range(5):
        _turn()
    assert len(ready) == 1


def test_a_failed_reattach_fails_whatever_the_reading():
    from tests.test_bunker_handshake import FakePool
    caps = ManualCaps()
    sessions = bunker.BunkerSessionPool(FakePool(), relay_caps=caps)
    errors = []
    profile = SimpleNamespace(user_pubkey=PK, bunker_pubkey="zz", bunker_relays=[NIP46],
                              local_secret_hex="11" * 32, signer="remote")
    sessions.get(profile, lambda client: None, errors.append)
    assert len(errors) == 1
    caps.asked[0][1]()
    assert len(errors) == 1


WINDOW = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
import main_window
from nostr.relay_caps import RelayCapsReader

w = main_window.MainWindow()
reader = w._session_pool._relay_caps
print("RESULT " + json.dumps({"reader": isinstance(reader, RelayCapsReader),
                              "parent": reader is not None and reader.parent() is w}))
"""


def test_the_main_window_reads_what_its_signer_connections_carry(tmp_path):
    from tests.app_process import run_window_script
    assert run_window_script(WINDOW, tmp_path) == {"reader": True, "parent": True}
