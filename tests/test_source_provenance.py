# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public availability is separate from a persisted signature or public flag."""
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from nostr.source_provenance import check_source_snapshot, check_linked_siblings
from tests.outbox_fakes import (PK, OTHER_PK, OTHER_SK, FakePool, FakeSubscription,
                               FakeRelayDirectory, HandPool, signed, settle)


@pytest.fixture(scope='module', autouse=True)
def app():
    application = QApplication.instance() or QApplication([])
    yield application


class SourcePool(FakePool):
    def subscribe(self, urls, filters):
        sub = FakeSubscription(urls, filters)
        def matches(event, f):
            return (event['kind'] in f.get('kinds', [event['kind']])
                    and event['pubkey'] in f.get('authors', [event['pubkey']])
                    and event['id'] in f.get('ids', [event['id']])
                    and all(any(t[0] == key[1:] and len(t) > 1 and t[1] in values for t in event['tags'])
                            for key, values in f.items() if key.startswith('#')))
        def answer():
            events = [e for e in self.stored.values() if any(matches(e, f) for f in filters)]
            for url in urls:
                sub.answer(url, *events)
        QTimer.singleShot(0, answer)
        return sub


def source(event):
    return {'address': f'30023:{event["pubkey"]}:source', 'eventId': event['id'],
            'content': event['content'], 'public': True, 'event': event}


def check(snapshot, *events):
    pool = SourcePool()
    pool.stored = {event['id']: event for event in events}
    results = []
    operation = check_source_snapshot(pool, FakeRelayDirectory({PK: ['wss://test.example']}), snapshot, results.append)
    settle(25)
    assert results, 'bounded source read must finish'
    return results[0], operation


def test_private_signed_event_and_persisted_public_flag_are_not_proof():
    event = signed(30023, [['d', 'source']], 'Private original')
    result, _op = check(source(event))
    assert not result['available']
    assert result['event'] is None


def test_exact_source_observed_publicly_is_verified_even_without_embedded_event():
    event = signed(30023, [['d', 'source']], 'Published original')
    snapshot = source(event)
    snapshot.pop('event')
    result, _op = check(snapshot, event)
    assert result['available'] and result['event'] == event
    assert result['confirmed_root'] == snapshot['address']
    assert not result['changed']


def test_a_reference_kept_without_its_text_is_read_back_by_its_id():
    # me68-red-a-4: a draft too long for the original's text keeps only
    # its fingerprint; the public original still confirms it.
    event = signed(30023, [['d', 'source']], 'Published original')
    snapshot = source(event)
    snapshot.pop('event')
    snapshot.pop('content')
    result, _op = check(snapshot, event)
    assert result['available'] and result['event'] == event


def test_source_pin_stays_old_and_only_translatable_changes_need_review():
    old = signed(30023, [['d', 'source'], ['title', 'Title']], 'Original')
    metadata_edit = signed(30023, [['d', 'source'], ['title', 'Title'], ['t', 'new']], 'Original', created_at=old['created_at'] + 1)
    result, _op = check(source(old), old, metadata_edit)
    assert result['event']['id'] == old['id']
    assert result['current']['id'] == metadata_edit['id']
    assert not result['changed']
    text_edit = signed(30023, [['d', 'source'], ['title', 'Title']], 'Changed text', created_at=old['created_at'] + 2)
    result, _op = check(source(old), old, text_edit)
    assert result['event']['id'] == old['id'] and result['changed']


def test_deleted_or_mismatched_snapshot_cannot_become_public_provenance():
    event = signed(30023, [['d', 'source']], 'Original')
    deletion = signed(5, [['e', event['id']]], '', created_at=event['created_at'] + 1)
    result, _op = check(source(event), event, deletion)
    assert not result['available']
    result, _op = check({**source(event), 'content': 'Another private snapshot'}, event)
    assert not result['available']


def test_foreign_root_needs_current_mutual_signed_approval():
    root_address = f'30023:{OTHER_PK}:root'
    event = signed(30023, [['d', 'source'], ['a', root_address, '', 'original']], 'Reference')
    root = signed(30023, [['d', 'root']], 'Root', sk=OTHER_SK)
    result, _op = check(source(event), event, root)
    assert result['available'] and not result['confirmed_root']
    approved = signed(30023, [['d', 'root'], ['a', source(event)['address'], '', 'translation']], 'Root', sk=OTHER_SK,
                      created_at=root['created_at'] + 1)
    result, _op = check(source(event), event, approved)
    assert result['confirmed_root'] == root_address


def test_event_id_only_deletions_of_newer_source_and_root_are_observed():
    old = signed(30023, [['d', 'source']], 'Reference')
    current = signed(30023, [['d', 'source']], 'Changed', created_at=old['created_at'] + 1)
    deletion = signed(5, [['e', current['id']]], '', created_at=current['created_at'] + 1)
    result, _op = check(source(old), old, current, deletion)
    assert not result['available']
    root = signed(30023, [['d', 'root']], 'Root')
    reference = signed(30023, [['d', 'source'], ['a', f'30023:{PK}:root', '', 'original']], 'Reference')
    deletion = signed(5, [['e', root['id']]], '', created_at=root['created_at'] + 1)
    result, _op = check(source(reference), root, reference, deletion)
    assert result['available'] and not result['confirmed_root']


def siblings(versions, *events):
    pool = SourcePool()
    pool.stored = {event['id']: event for event in events}
    results = []
    operation = check_linked_siblings(pool, FakeRelayDirectory({PK: ['wss://test.example']}),
        versions, PK, results.append, exclude='de')
    settle(50)
    assert results
    return results[0], operation


def test_closed_siblings_reveal_the_established_public_root_for_late_original():
    english = signed(30023, [['d', 'en']], 'English')
    address = f'30023:{PK}:en'
    spanish = signed(30023, [['d', 'es'], ['a', address, '', 'original']], 'Spanish')
    result, _operation = siblings({'de': 'de', 'en': 'en', 'es': 'es'}, english, spanish)
    assert result['root'] == address
    assert len(result['events']) == 2
    assert not result['conflict']


def test_multiple_public_singletons_promote_by_backlinks_without_guessing_a_group():
    english = signed(30023, [['d', 'en']], 'English')
    spanish = signed(30023, [['d', 'es']], 'Spanish')
    result, _operation = siblings({'de': 'de', 'en': 'en', 'es': 'es'}, english, spanish)
    assert result['root'] == ''
    assert set(result['translations']) == {f'30023:{PK}:en', f'30023:{PK}:es'}


def test_conflicting_established_groups_are_not_merged_by_a_private_record():
    english = signed(30023, [['d', 'en']], 'English')
    spanish = signed(30023, [['d', 'es'], ['a', f'30023:{PK}:en', '', 'original']], 'Spanish')
    thai = signed(30023, [['d', 'th']], 'Thai')
    lao = signed(30023, [['d', 'lo'], ['a', f'30023:{PK}:th', '', 'original']], 'Lao')
    result, _operation = siblings({'de': 'de', 'en': 'en', 'es': 'es', 'th': 'th', 'lo': 'lo'}, english, spanish, thai, lao)
    assert result['conflict']
    assert not result['root'] and not result['translations']


def test_backlink_only_member_uses_verified_current_root_instead_of_becoming_root():
    english = signed(30023, [['d', 'source']], 'English')
    german = signed(30023, [['d', 'german'], ['a', source(english)['address'], '', 'translation']], 'German')
    result, _op = check(source(english), english, german)
    assert result['confirmed_root'] == f'30023:{PK}:german'
    # An old backlink on a replaced root does not grant current membership.
    revoked = signed(30023, [['d', 'german']], 'German', created_at=german['created_at'] + 1)
    result, _op = check(source(english), english, german, revoked)
    assert result['available'] and not result['confirmed_root']


def test_conflicting_incoming_roots_cannot_grant_an_implicit_group_claim():
    english = signed(30023, [['d', 'source']], 'English')
    german = signed(30023, [['d', 'german'], ['a', source(english)['address'], '', 'translation']], 'German')
    french = signed(30023, [['d', 'french'], ['a', source(english)['address'], '', 'translation']], 'French')
    result, _op = check(source(english), english, german, french)
    assert result['available'] and not result['confirmed_root']


def test_late_original_preserves_incoming_root_absent_from_private_versions():
    """A verified root can be learned through a sibling, without a local tab."""
    spanish_address = f'30023:{PK}:es'
    english = signed(30023, [['d', 'en'], ['a', spanish_address, '', 'translation']], 'English')
    spanish = signed(30023, [['d', 'es']], 'Spanish')
    result, _operation = siblings({'de': 'de', 'es': 'es'}, english, spanish)
    assert result['root'] == f'30023:{PK}:en'
    assert not result['conflict'] and not result['translations']
    assert [event['id'] for event in result['events']] == [spanish['id']]


def test_external_incoming_roots_are_not_merged_by_a_late_original():
    english = signed(30023, [['d', 'en'], ['a', f'30023:{PK}:es', '', 'translation']], 'English')
    spanish = signed(30023, [['d', 'es']], 'Spanish')
    thai = signed(30023, [['d', 'th'], ['a', f'30023:{PK}:lo', '', 'translation']], 'Thai')
    lao = signed(30023, [['d', 'lo']], 'Lao')
    result, _operation = siblings({'de': 'de', 'es': 'es', 'lo': 'lo'}, english, spanish, thai, lao)
    assert result['conflict']
    assert not result['root'] and not result['translations']


@pytest.mark.parametrize('marker,foreign,available', [('e', False, False), ('a', False, True), ('e', True, True)])
def test_exact_event_deletion_is_not_limited_by_the_article_timestamp(marker, foreign, available):
    event = signed(30023, [['d', 'source']], 'Reference', created_at=200)
    target = event['id'] if marker == 'e' else source(event)['address']
    deletion = signed(5, [[marker, target]], '', created_at=199, **({'sk': OTHER_SK} if foreign else {}))
    result, _operation = check(source(event), event, deletion)
    assert result['available'] is available


def test_the_first_relay_with_the_reference_decides_and_a_dead_relay_stays_quiet():
    event = signed(30023, [['d', 'source']], 'Published original')
    pool, results = HandPool(), []
    check_source_snapshot(pool, FakeRelayDirectory({PK: ['wss://one.example']}), source(event),
                          results.append)
    settle()
    reference = pool.subs[-1]
    assert len(reference.urls) > 2
    reference.answer(reference.urls[0], event)          # the others say nothing yet
    reference.fail(reference.urls[-1])
    settle()
    deletions = pool.subs[-1]
    assert deletions is not reference and not results
    for url in deletions.urls[:-1]:
        deletions.answer(url)
    deletions.fail(deletions.urls[-1])
    settle()
    assert results and results[0]['available'] and not results[0]['deleted']


def test_a_deletion_only_a_slower_relay_has_is_still_heard():
    # me68-verify-6: the first relay with the original decides that it is
    # there; whether it was deleted is heard from every relay.
    event = signed(30023, [['d', 'source']], 'Published original')
    gone = signed(5, [['e', event['id']], ['a', f"30023:{PK}:source"]], '')
    pool, results = HandPool(), []
    check_source_snapshot(pool, FakeRelayDirectory({PK: ['wss://one.example']}), source(event),
                          results.append)
    settle()
    reference = pool.subs[-1]
    reference.answer(reference.urls[0], event)
    settle()
    deletions = pool.subs[-1]
    assert deletions is not reference
    deletions.answer(deletions.urls[0])
    settle()
    assert not results                          # the others are still heard
    deletions.answer(deletions.urls[1], gone)
    for url in deletions.urls[2:]:
        deletions.answer(url)
    settle()
    assert results and not results[0]['available'] and results[0]['deleted']
