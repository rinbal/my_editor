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
CONNECTION_SIGN = ("The connection to your signer app carries only short messages: shorten "
                   "this text or split it into parts.")


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
        "This is too long to be signed through your signer app:")


def test_a_draft_the_connection_cannot_carry_is_said_plainly():
    words = signer_limits.size_problem(CAPPED, draft="x" * 30_000)
    assert words == ("The connection to your signer app carries only short messages: split "
                     "this draft into smaller ones or publish it directly.")
    assert "30" not in words                                # never the numbers
    assert signer_limits.size_problem(OPEN, draft="x" * 30_000) is None


def test_the_words_are_the_contracts_in_german():
    import i18n
    i18n.install("de")
    try:
        words = signer_limits.size_problem(
            CAPPED, event={"kind": 1, "content": "x" * 45_000, "tags": [], "created_at": 1})
    finally:
        i18n.install("en")
    # The contract's plain consequence, in one sentence with what to do
    # (lessons 36).
    assert words == ("Die Verbindung zu deiner Signer-App trägt nur kurze Nachrichten: "
                     "Kürze diesen Text oder teile ihn in Teile auf.")


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


def test_every_page_fits_a_capped_connection_whatever_the_writers():
    # Contract 12.5 "Pages fit every connection" (007ded2): a key kept
    # here and an uncapped connection write pages a connection capped at
    # 65,536 characters can ask for and carry back, so every device reads
    # every page.
    entries = [f'item "{index}" ' + "y" * 80 for index in range(1_500)]
    key = bytes(range(32))
    for profile in (LOCAL, OPEN, CAPPED, None):
        for text in _pages_of(profile, entries):
            assert len(text.encode("utf-8")) <= 28_672
            assert answer_size.decrypt_answer_size(text) <= 40_896
            assert answer_size.encrypt_request_size(text) <= 40_960
            assert signer_limits.can_ask_to_decrypt(CAPPED, crypto.encrypt(text, key))
    assert _pages_of(LOCAL, entries) == _pages_of(OPEN, entries) == _pages_of(None, entries)


def test_the_page_bounds_are_what_a_capped_connection_carries():
    # The contract's numbers follow from the measure: a page's plaintext
    # and answer are what a connection capped at 65,536 characters carries.
    from nostr.paged_list import PAGE_ANSWER_MAX, PAGE_PAYLOAD_MAX, PAGE_PLAINTEXT_MAX
    assert PAGE_PLAINTEXT_MAX == connection_plaintext_limit([NIP46]) == 28_672
    assert PAGE_ANSWER_MAX == connection_answer_limit([NIP46]) == 40_896
    assert crypto.payload_length(PAGE_PLAINTEXT_MAX) == PAGE_PAYLOAD_MAX == 38_320
    assert crypto.payload_length(PAGE_PLAINTEXT_MAX + 1) == 43_780     # pads to 32,768


def test_a_connection_capped_lower_splits_its_pages_smaller_still():
    entries = [f'item "{index}" ' + "y" * 80 for index in range(1_500)]
    note_relay_cap("wss://small.example", 40_000)
    small = SimpleNamespace(user_pubkey=PK, signer="remote", bunker_relays=["wss://small.example"])
    small_pages = _pages_of(small, entries)
    assert len(small_pages) > len(_pages_of(OPEN, entries))
    for text in small_pages:
        assert answer_size.encrypt_request_size(text) <= \
            connection_message_limit(["wss://small.example"])
        assert answer_size.decrypt_answer_size(text) <= \
            connection_answer_limit(["wss://small.example"])


def test_a_capped_accounts_pages_fit_the_request_that_carries_them():
    # One quote a time: the page fills up to the answer's bound, and the
    # request, 16 bytes tighter, must still hold it.
    for text in _pages_of(CAPPED, ['"'] * 10_000):
        assert answer_size.encrypt_request_size(text) <= 40_960
        assert answer_size.decrypt_answer_size(text) <= 40_896


def test_every_account_writes_its_list_in_pages_a_capped_connection_carries():
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
    assert largest["capped"] <= 28_672 and largest["open"] <= 28_672


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


# -- every page fits every connection (contract 007ded2) -------------------------------

def test_a_page_may_be_28672_bytes_and_its_answer_40896_and_not_one_more():
    from nostr.paged_list import page_fits
    assert page_fits("x" * 28_672) and not page_fits("x" * 28_673)
    # Six bytes a "<" in the answer: the answer decides first.
    fitting = max(n for n in range(6_700, 6_900)
                  if answer_size.decrypt_answer_size("<" * n) <= 40_896)
    assert page_fits("<" * fitting) and not page_fits("<" * (fitting + 1))


def _write(pages_of, profile):
    """A write whose split gave ``pages_of``'s pages, with the key kept
    here (whose own bound would let a 65,535-byte page through)."""
    from nostr.encrypted_lists import EncryptedList
    from tests.outbox_fakes import settle
    from tests.test_encrypted_lists import Relay, submission, submissions_list
    from tests.test_imports_subscriptions import FakeSigner

    class Counting(FakeSigner):
        def __init__(self):
            super().__init__()
            self.encrypted = []

        def nip44_encrypt_self(self, plaintext, on_success, on_failure, **kw):
            self.encrypted.append(plaintext)
            super().nip44_encrypt_self(plaintext, on_success, on_failure, **kw)

    relay, signer, results = Relay(), Counting(), []
    store = submissions_list(relay, signer=signer)
    original = EncryptedList._plaintexts
    EncryptedList._plaintexts = lambda self, value, now, profile=None: pages_of(
        original(self, value, now, profile))
    try:
        store.publish(profile, state=lambda: (None, [submission(1)], None),
                      on_done=results.append, on_merged=lambda *_args: None)
        settle(40)
    finally:
        EncryptedList._plaintexts = original
    return results[-1], signer.encrypted, relay


def test_a_page_of_28673_bytes_is_never_written():
    from nostr.list_results import PublishStatus
    from tests.outbox_fakes import PK as OWN

    def padded_to(size):
        def pages_of(texts):
            page = json.loads(texts[0])
            page["items"].append("x" * (size - len(texts[0].encode("utf-8")) - 3))
            text = json.dumps(page, separators=(",", ":"))
            assert len(text.encode("utf-8")) == size
            return [text]
        return pages_of

    local = SimpleNamespace(user_pubkey=OWN, signer="local", bunker_relays=[])
    result, encrypted, relay = _write(padded_to(28_673), local)
    assert result.status is PublishStatus.FAILED and "larger than a page may be" in result.reason
    assert encrypted == [] and relay.published == []
    result, encrypted, _relay = _write(padded_to(28_672), local)
    assert result.status is PublishStatus.SENT and len(encrypted) == 1


class ListSigner(EncryptingSigner):
    """Answers nip44_decrypt too, with the account's key; over a relay
    that caps content at ``cap`` characters (relay.nip46.com: 65,536), a
    longer answer never arrives."""

    def __init__(self, pool, cap=None):
        super().__init__(pool)
        self.cap = cap

    def _on_event(self, event):
        if event.get("pubkey") == self.pk:
            return
        try:
            payload = json.loads(crypto.decrypt(event["content"], self.conv))
        except (ValueError, json.JSONDecodeError, KeyError):
            payload = {}
        if payload.get("method") != "nip44_decrypt":
            super()._on_event(event)
            return
        self.seen.append(payload)
        peer, ciphertext = payload["params"]
        plaintext = crypto.decrypt_from(ciphertext, self.user_sk, bytes.fromhex(peer))
        self.send({"id": payload["id"], "result": plaintext})

    def send_raw(self, content):
        if self.cap is None or len(content) <= self.cap:
            super().send_raw(content)


def _paired(relay: str, cap=None):
    """A real client paired with a ``ListSigner`` over ``relay``, and the
    account it signs for, as a profile."""
    from tests.test_bunker_handshake import FakePool
    from urllib.parse import quote
    pool = FakePool()
    signer = ListSigner(pool, cap)
    client = bunker.BunkerClient(pool)
    local_sk = crypto.generate_secret_key()
    signer.watch(crypto.get_public_key(local_sk).hex())
    outcome = {}
    client.connect_to_bunker(f"bunker://{signer.pk}?relay={quote(relay, safe='')}&secret=abc",
                             on_success=lambda pk: outcome.setdefault("ok", pk),
                             on_failure=lambda r: outcome.setdefault("err", r),
                             local_sk=local_sk)
    assert outcome.get("ok") == signer.user_pk, outcome
    profile = SimpleNamespace(user_pubkey=signer.user_pk, signer="remote", bunker_relays=[relay])
    return client, signer, profile


WORDS_TOO_LONG = "too long words"


def _account_list(relay, client):
    from nostr.encrypted_lists import EncryptedList
    from nostr.list_results import ListWords
    from tests.outbox_fakes import FakeRelayDirectory
    from tests.test_encrypted_lists import NOW, SUBS, by_sid
    from tests.test_imports_subscriptions import FakeSessionPool
    words = ListWords(unreadable="unreadable words", signer_silent="silent words",
                      old_encryption="old words", too_long_for_connection=WORDS_TOO_LONG)
    return EncryptedList(
        d_tag=SUBS, decode=lambda payload: by_sid(None, payload["items"], []),
        encode=lambda value, now: {"items": list(value)}, merge=by_sid, words=words,
        session_pool=FakeSessionPool(client), relay_directory=FakeRelayDirectory(),
        query=relay, publisher=relay, clock=lambda: NOW, paging=Paging(field="items"))


def _read_back(store, profile):
    from tests.outbox_fakes import settle
    answers = []
    store.relays(profile, lambda relays: store.read(profile, relays, answers.append),
                 reading=True)
    for _ in range(200):
        if answers:
            break
        settle(5)
    assert answers, "the read never finished"
    return answers[-1]


def test_a_large_list_written_anywhere_reads_back_over_a_capped_connection():
    # 007ded2: a list a key kept here wrote reads back, every page of it,
    # through a signer app whose only relay carries 65,536 characters.
    from nostr.list_results import PublishStatus, ReadStatus
    from nostr.local_signer import LocalSigner
    from tests.outbox_fakes import settle
    from tests.test_encrypted_lists import SUBS, Relay, submission
    from tests.test_imports_subscriptions import FakeSessionPool
    client, signer, capped = _paired(NIP46, cap=65_536)
    relay = Relay()
    writer = _account_list(relay, None)
    writer._session_pool = FakeSessionPool(LocalSigner(signer.user_sk))
    here = SimpleNamespace(user_pubkey=signer.user_pk, signer="local", bunker_relays=[])
    items = [submission(n) for n in range(600)]
    results = []
    writer.publish(here, state=lambda: (None, items, None), on_done=results.append,
                   on_merged=lambda *_args: None)
    for _ in range(200):
        if results:
            break
        settle(5)
    assert results and results[-1].status is PublishStatus.SENT, results
    pages = sum(1 for d in relay.stored if d.startswith(SUBS))
    assert pages >= 8
    answer = _read_back(_account_list(relay, client), capped)
    assert answer.status is ReadStatus.COMPLETE, answer.words
    assert sorted(i["sid"] for i in answer.value) == sorted(i["sid"] for i in items)
    assert sum(1 for asked in signer.seen if asked["method"] == "nip44_decrypt") == pages


def _legacy(relay, signer, items):
    """The list as written before 007ded2: pages of up to 32,000 bytes."""
    from nostr import events
    from nostr.list_relays import LIST_KIND
    from nostr.paged_list import page_d_tag
    from tests.test_encrypted_lists import NOW, SUBS
    texts = split({"items": items}, Paging("items", max_bytes=32_000, answer_max=65_471))
    assert max(len(text.encode("utf-8")) for text in texts) > 30_000
    user_pk = bytes.fromhex(signer.user_pk)
    for index, text in enumerate(texts):
        relay.stored[page_d_tag(SUBS, index)] = events.sign_event(
            {"kind": LIST_KIND, "content": crypto.encrypt_to(text, signer.user_sk, user_pk),
             "created_at": NOW - 100, "tags": [["d", page_d_tag(SUBS, index)],
                                               ["encrypted", "nip44"]]}, signer.user_sk)
    return len(texts)


def test_a_page_written_before_that_the_connection_cannot_ask_for_is_said_never_complete():
    # 12.5: the reader says so in plain words, never reads the list as
    # complete, and never writes over it; the signer is not asked for it.
    from nostr.list_results import PublishStatus, ReadStatus
    from tests.outbox_fakes import settle
    from tests.test_encrypted_lists import Relay, submission
    client, signer, capped = _paired(NIP46, cap=65_536)
    relay = Relay()
    _legacy(relay, signer, [submission(n) for n in range(300)])
    store = _account_list(relay, client)
    statuses = []
    store._on_status = statuses.append
    answer = _read_back(store, capped)
    assert answer.status is ReadStatus.BLOCKED and answer.words == WORDS_TOO_LONG
    assert statuses == [WORDS_TOO_LONG]
    assert not any(asked["method"] == "nip44_decrypt" for asked in signer.seen)
    before = dict(relay.stored)
    results = []
    store.publish(capped, state=lambda: (None, [submission(999)], None),
                  on_done=results.append, on_merged=lambda *_args: None)
    for _ in range(100):
        if results:
            break
        settle(5)
    assert results and results[-1].status is PublishStatus.BLOCKED
    assert relay.stored == before


def test_the_same_page_is_read_over_a_connection_that_carries_it():
    from nostr.list_results import ReadStatus
    from tests.test_encrypted_lists import Relay, submission
    client, signer, uncapped = _paired("wss://nos.lol")
    relay = Relay()
    items = [submission(n) for n in range(300)]
    pages = _legacy(relay, signer, items)
    answer = _read_back(_account_list(relay, client), uncapped)
    assert answer.status is ReadStatus.COMPLETE
    assert len(answer.value) == len(items)
    assert sum(1 for asked in signer.seen if asked["method"] == "nip44_decrypt") == pages


def test_without_its_own_words_a_list_says_it_could_not_be_read():
    from nostr.list_results import ListWords, ReadStatus
    from tests.test_encrypted_lists import Relay, submission
    client, signer, capped = _paired(NIP46, cap=65_536)
    relay = Relay()
    _legacy(relay, signer, [submission(n) for n in range(300)])
    store = _account_list(relay, client)
    store._words = ListWords(unreadable="unreadable words", signer_silent="silent words",
                             old_encryption="old words")
    answer = _read_back(store, capped)
    assert answer.status is ReadStatus.BLOCKED and answer.words == "unreadable words"


def test_every_list_says_when_its_connection_cannot_carry_a_page(tmp_path):
    from nostr.imports import subscriptions
    from nostr.standup import lists
    from tests.test_imports_subscriptions import make_store
    assert lists._submissions_words().too_long_for_connection == \
        lists.SUBMISSIONS_TOO_LONG_FOR_CONNECTION
    assert lists._read_state_words().too_long_for_connection == \
        lists.READ_STATE_TOO_LONG_FOR_CONNECTION
    store = make_store(tmp_path)[0]
    assert store._list._words.too_long_for_connection == \
        store._legacy_list._words.too_long_for_connection == \
        subscriptions._TOO_LONG_FOR_CONNECTION
    assert subscriptions._TOO_LONG_FOR_CONNECTION.startswith(
        "The connection to your signer app carries only short messages")
    for words in (lists.SUBMISSIONS_TOO_LONG_FOR_CONNECTION,
                  lists.READ_STATE_TOO_LONG_FOR_CONNECTION, subscriptions._TOO_LONG_FOR_CONNECTION):
        assert "Connect your signer app again" in words
        assert not any(term in words for term in ("relay", "NIP", "28", "byte", "page"))


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
    """A session pool whose bound reading is held: ``asked`` the profiles
    and their ``done``; ``got`` any signer asked for (none may be)."""
    def __init__(self):
        self.asked = []
        self.got = []

    def read_bounds(self, profile, done):
        self.asked.append((profile, done))

    def get(self, profile, on_ready, on_error):
        self.got.append(profile)


def _standup_writer(sessions, account):
    from nostr.ui.standup_submit import StandupWriter
    return StandupWriter(session_pool=sessions, relay_pool=None, relay_directory=None,
                         media_store=None, server="https://s.example",
                         uploads=lambda *a: None, accounts=lambda _profile: account)


def test_the_writer_waits_for_the_relays_documents_alone():
    # RG-M2r2b 13: not the signer reattaching, which may fail before the
    # reading ends (lesson 29); the bound is what the documents say.
    sessions = HeldSessions()
    account = SimpleNamespace(writer=PK, stopped=False, runs=[], deleteLater=lambda: None)
    told = []
    _standup_writer(sessions, account).signer_bound(CAPPED, lambda: told.append(1))
    (profile, done), = sessions.asked
    assert profile is CAPPED and told == [] and sessions.got == []
    done()
    assert told == [1]


def test_the_pool_answers_the_bound_once_the_documents_are_read_not_the_reattach():
    from nostr.bunker import BunkerSessionPool
    nam = FakeNam([_document(40_000)])
    pool = BunkerSessionPool(None, relay_caps=relay_caps.RelayCapsReader(nam=nam))
    pool.get = lambda *a, **k: pytest.fail("a signer was asked for")
    told = []
    profile = SimpleNamespace(user_pubkey=PK, signer="remote", bunker_relays=["wss://c.example"])
    pool.read_bounds(profile, lambda: told.append(connection_message_limit(["wss://c.example"])))
    _turn()
    assert told == []                                      # still reading
    nam.settle()
    _turn()
    assert told == [connection_message_limit(["wss://c.example"])] and told[0] < 65_536
    pool.read_bounds(profile, lambda: told.append("known"))   # known: at once, later
    assert told[-1] != "known"
    _turn()
    assert told[-1] == "known" and len(nam.issued) == 1


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
    sessions.asked[0][1]()
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


def test_only_a_public_wss_relay_is_read_and_the_others_count_as_unreadable():
    # RG-M2r2b 11: outbox.policy.is_public_relay decides, as for every
    # relay someone else named; a relay not asked is unreadable, so the
    # bound is known without it.
    assert relay_caps.document_url("wss://relay.example:7777/x") == \
        "https://relay.example:7777/x"
    others = ["ws://relay.example:7777/x", "wss://localhost:7777", "wss://127.0.0.1",
              "wss://10.0.0.8", "wss://nas.local", "wss://router", "wss://[::1]",
              "wss://169.254.169.254"]
    for relay in others + ["https://relay.example", "nonsense"]:
        assert relay_caps.document_url(relay) is None, relay
    nam = FakeNam()
    told = []
    relay_caps.RelayCapsReader(nam=nam).read(others + ["https://relay.example", "nonsense"],
                                             lambda: told.append(1))
    _turn()
    assert nam.calls == [] and told == [1]
    local = SimpleNamespace(user_pubkey=PK, signer="remote", bunker_relays=others)
    assert signer_limits.bound_known(local)
    assert not any(signer_limits.relay_read(relay) for relay in others)
    assert connection_message_limit(others) == signer_limits.NIP44_MESSAGE_MAX


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


def test_a_relay_that_says_nothing_is_given_up_on():
    # No wall clock (lesson 34): the reader's own deadline is run out.
    from PySide6.QtCore import QTimer
    nam = FakeNam()
    told = []
    relay_caps.RelayCapsReader(nam=nam).read(["wss://silent.example"], lambda: told.append(1))
    reply = nam.issued[0]
    deadline, = reply.findChildren(QTimer)
    assert deadline.isActive() and deadline.isSingleShot()
    assert deadline.interval() == relay_caps.NIP11_TIMEOUT_MS
    assert not reply.aborted and told == []
    deadline.timeout.emit()
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


def test_the_page_bounds_never_pass_the_page_caps_whatever_signs():
    # RG-M2r2b 5: one cap for every signer type, in page_bounds itself: a
    # key kept here and a connection without caps write the same pages a
    # capped connection reads.
    from nostr.paged_list import PAGE_ANSWER_MAX, PAGE_PLAINTEXT_MAX
    for profile in (LOCAL, OPEN, CAPPED):
        bounds = signer_limits.page_bounds(profile)
        assert bounds["plaintext_max"] <= PAGE_PLAINTEXT_MAX, profile
        assert bounds["answer_max"] <= PAGE_ANSWER_MAX, profile
    for profile in (LOCAL, OPEN):
        assert signer_limits.page_bounds(profile)["plaintext_max"] == PAGE_PLAINTEXT_MAX
        assert signer_limits.page_bounds(profile)["answer_max"] == PAGE_ANSWER_MAX


def test_one_answer_says_when_a_connections_bound_is_known():
    # RG-M2r2b 12: the reader and a writer's check ask signer_limits alone.
    known = signer_limits.relays_bound_known
    assert known([]) and known(["nonsense"])
    assert not known([NIP46, "wss://nos.lol"])
    note_relay_cap(NIP46, 40_000)
    assert not known([NIP46, "wss://nos.lol"])
    note_relay_cap("wss://nos.lol", None, read=False)
    assert known([NIP46, "wss://nos.lol"])
    signer_limits.forget_relay_caps()
    note_relay_cap("wss://nos.lol", None)                   # one carries all
    assert known([NIP46, "wss://nos.lol"])
    assert signer_limits.bound_known(MIXED) == known(MIXED.bunker_relays)


def test_a_waiter_goes_on_once_nothing_more_is_read():
    # A relay read long ago may leave the bounded record while another is
    # still read: once none is read any more, the waiter is told.
    nam = FakeNam([_document(40_000), _document(30_000)])
    told = []
    reader = relay_caps.RelayCapsReader(nam=nam)
    reader.read(["wss://one.example", "wss://two.example"], lambda: told.append(1))
    nam.settle_one()
    for index in range(300):                                # the record keeps 256
        note_relay_cap(f"wss://r{index}.example", 50_000)
    assert not signer_limits.relay_read("wss://one.example")
    nam.settle_one()
    _turn()
    assert told == [1]
