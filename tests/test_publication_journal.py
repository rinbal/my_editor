# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Restart, partial-pair failure and receipt retention use real signatures."""
from copy import deepcopy

import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

from nostr.publication_journal import PublicationJournal
from nostr.multilingual_publish import PairPublication
from nostr.publisher import PublishJob
from tests.outbox_fakes import (PK, OTHER_PK, FakeClient, FakePool,
                                FakeRelayDirectory, FakeSessionPool, Profile,
                                FakeSubscription, signed, settle)


class ArticlePool(FakePool):
    def subscribe(self, urls, filters):
        sub = FakeSubscription(urls, filters)
        self.subscriptions.append((list(urls), filters))
        def answer():
            matches = [event for event in self.stored.values()
                       if any(event['kind'] in f.get('kinds', [])
                              and event['pubkey'] in f.get('authors', [])
                              and any(t[0] == 'd' and t[1] in f.get('#d', []) for t in event['tags'])
                              for f in filters)]
            for url in urls:
                sub.answer(url, *matches)
        QTimer.singleShot(0, answer)
        return sub


@pytest.fixture(scope="module", autouse=True)
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


def article(d="source", content="Reference"):
    return {"kind": 30023, "pubkey": PK, "created_at": 1_800_000_000,
            "tags": [["d", d], ["published_at", "1800000000"]], "content": content}


def options(journal, pool=None, client=None):
    return dict(journal=journal, relay_pool=pool or FakePool(),
                relay_directory=FakeRelayDirectory({PK: ["wss://test.example"]}),
                session_pool=FakeSessionPool(client or FakeClient()), profile=Profile(),
                extra_relays=["wss://test.example"])


def test_restart_keeps_exact_signed_payload_and_scopes_account(tmp_path):
    journal = PublicationJournal(tmp_path / "publications.sqlite3")
    event = signed(30023, article()["tags"], "Reference")
    journal.remember(event)
    journal.dispatching(event["id"])
    reopened = PublicationJournal(journal.path)
    requested = {**article(), "created_at": event["created_at"] + 20}
    requested["tags"] = [["d", "source"], ["published_at", "1800000100"]]
    assert reopened.find(requested)["event"] == event
    assert len(reopened.pending(PK)) == 1
    assert not reopened.pending(OTHER_PK)
    assert reopened.find({**requested, "pubkey": OTHER_PK}) is None


def test_acceptance_survives_later_timeout_and_new_revision_allows_revert(tmp_path):
    journal = PublicationJournal(tmp_path / "publications.sqlite3")
    event = signed(30023, article()["tags"], "Reference")
    journal.remember(event)
    journal.accepted(event["id"], "wss://test.example")
    journal.answered(event["id"], [("wss://test.example", False, "timeout")])
    assert journal.find(article())["status"] == "accepted"
    newer = signed(30023, article()["tags"], "Changed", created_at=event["created_at"] + 1)
    journal.remember(newer)
    assert journal.find(article()) is None


def test_failed_first_attempt_retries_without_signer_after_restart(tmp_path):
    path = tmp_path / "publications.sqlite3"
    client = FakeClient()
    class RefusingPool(FakePool):
        def publish(self, urls, event):
            self.refuse = set(urls)
            return super().publish(urls, event)
    rejected = RefusingPool()
    first = PublishJob(unsigned_event=article(), **options(PublicationJournal(path), rejected, client))
    first.start()
    settle()
    assert len(client.requests) == 1
    assert rejected.published
    later = FakeClient(fail="must never be called")
    accepted = FakePool()
    resumed = PublishJob(unsigned_event=article(), **options(PublicationJournal(path), accepted, later))
    resumed.start()
    settle()
    assert not later.requests
    assert accepted.published[0][1] == rejected.published[0][1]
    complete = PublishJob(unsigned_event=article(), **options(PublicationJournal(path), accepted, later))
    complete.start()
    settle()
    assert len(accepted.published) == 1  # successful event is not resent


def test_storage_failure_prevents_any_dispatch(tmp_path, monkeypatch):
    journal = PublicationJournal(tmp_path / "publications.sqlite3")
    monkeypatch.setattr(journal, "remember", lambda _event: (_ for _ in ()).throw(OSError("disk full")))
    pool = FakePool()
    job = PublishJob(unsigned_event=article(), **options(journal, pool))
    failed = []
    job.failed.connect(failed.append)
    job.start()
    settle()
    assert failed
    assert not pool.published


def test_pair_retries_only_target_and_pins_exact_source(tmp_path):
    class PartialPool(ArticlePool):
        reject_target = True
        def publish(self, urls, event):
            self.refuse = set(urls) if self.reject_target and any(t[:2] == ["d", "target"] for t in event["tags"]) else set()
            return super().publish(urls, event)
    journal = PublicationJournal(tmp_path / "publications.sqlite3")
    pool = PartialPool()
    client = FakeClient()
    observed = []
    def target(source):
        observed.append(deepcopy(source))
        return {**article("target", "Translation"), "tags": [["d", "target"],
                ["e", source["id"], "", "source", PK]]}
    kwargs = options(journal, pool, client)
    kwargs.pop("extra_relays")
    # PairPublication obtains routes from the ordinary directory.
    kwargs["entitled_relays"] = ["wss://test.example"]
    pair = PairPublication(source_event=article(), target_factory=target, **kwargs)
    pair.start()
    settle(20)
    assert len(pool.published) == 2
    assert observed[0] == pool.published[0][1]
    pool.refuse.clear()
    pool.reject_target = False
    assert pair.send_again()
    settle(20)
    assert len(client.requests) == 2
    assert len(pool.published) == 3
    assert pool.published[-1][1] == pool.published[-2][1]
    assert pair.signed_event == pool.published[-1][1]


def test_preflight_rejects_newer_remote_revision_without_dispatch(tmp_path):
    pool = ArticlePool()
    old = signed(30023, article()["tags"], "Old", created_at=1_700_000_000)
    newer = signed(30023, article()["tags"], "New remote text", created_at=1_700_000_001)
    pool.stored[newer['id']] = newer
    job = PublishJob(unsigned_event=article(), expected_event_id=old['id'],
                     **options(PublicationJournal(tmp_path / 'journal.sqlite3'), pool))
    errors = []
    job.failed.connect(errors.append)
    job.start()
    settle(15)
    assert errors and 'changed elsewhere' in job.failure_words
    assert not pool.published


def test_preflight_recognizes_an_accepted_event_without_sending_again(tmp_path):
    journal = PublicationJournal(tmp_path / 'journal.sqlite3')
    event = signed(30023, article()["tags"], 'Reference')
    journal.remember(event)  # Simulate lost receipt, still 'signed' locally.
    pool = ArticlePool()
    pool.stored[event['id']] = event
    job = PublishJob(unsigned_event=article(), expected_event_id=None, **options(journal, pool))
    results = []
    job.completed.connect(results.append)
    job.start()
    settle(15)
    assert results and any(item[1] for item in results[0])
    assert not pool.published
    assert journal.find(article())['status'] == 'accepted'


def test_deliberate_retry_rechecks_after_a_preflight_failure(tmp_path):
    pool = ArticlePool()
    other = signed(30023, article()['tags'], 'Other remote text', created_at=1_700_000_000)
    pool.stored[other['id']] = other
    job = PublishJob(unsigned_event=article(), expected_event_id=None,
                     **options(PublicationJournal(tmp_path / 'journal.sqlite3'), pool))
    job.start()
    settle(15)
    assert job.failure_words and not pool.published
    pool.stored.clear()
    assert job.send_again()
    settle(15)
    assert len(pool.published) == 1


def test_receipt_failure_after_verified_publication_does_not_hang(tmp_path, monkeypatch):
    journal = PublicationJournal(tmp_path / 'journal.sqlite3')
    event = signed(30023, article()['tags'], 'Reference')
    journal.remember(event)
    pool = ArticlePool()
    pool.stored[event['id']] = event
    monkeypatch.setattr(journal, 'answered', lambda *_args: (_ for _ in ()).throw(OSError('disk full')))
    job = PublishJob(unsigned_event=article(), expected_event_id=None, **options(journal, pool))
    results = []
    job.completed.connect(results.append)
    job.start()
    settle(15)
    assert results and not pool.published


@pytest.mark.parametrize("elsewhere", ["deleted", "newer"])
def test_identical_text_goes_out_again_after_a_deletion_or_a_newer_version(tmp_path, elsewhere):
    # me68-red-a-6: the journal's accepted signature is reused only while
    # it is the one public; else the same text is signed anew.
    journal = PublicationJournal(tmp_path / 'journal.sqlite3')
    event = signed(30023, article()["tags"], 'Reference', created_at=1_700_000_000)
    journal.remember(event)
    journal.accepted(event['id'], 'wss://test.example')
    pool, client = ArticlePool(), FakeClient()
    expected = event['id']
    if elsewhere == "newer":
        newer = signed(30023, article()["tags"], 'Written on the web', created_at=1_800_000_500)
        pool.stored[newer['id']] = newer
        expected = newer['id']                      # opened its latest version
    job = PublishJob(unsigned_event={**article(), "content": "Reference"},
                     expected_event_id=expected, **options(journal, pool, client))
    job.start()
    settle(20)
    assert len(client.requests) == 1 and len(pool.published) == 1
    sent = pool.published[0][1]
    assert sent['content'] == 'Reference' and sent['id'] != event['id']
    if elsewhere == "newer":
        assert sent['created_at'] > newer['created_at']
