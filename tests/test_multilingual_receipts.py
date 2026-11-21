# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accepted publication receipts belong to the reviewed documents and owner."""
from tests.app_process import run_window_script

SCRIPT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication
from nostr.article_details import ArticleDetails
from nostr.profiles import Profile
from tests.outbox_fakes import PK, OTHER_PK, OTHER_SK, signed
app = QApplication(sys.argv[:1])
import main_window
main_window.MainWindow._maybe_auto_check_for_updates = lambda self: None
w = main_window.MainWindow()
profile = Profile(user_pubkey=PK, bunker_pubkey='', bunker_relays=[], local_secret_hex='')
w._profile_store.default = lambda: profile
w.nostr_state._active = True
source = w.new_tab()
source.insertPlainText('Original')
w._set_article_details(source, ArticleDetails(identifier='group-source', title='Original').with_language('de'))
target = w._multilingual.open_language(source, 'en')
target.insertPlainText('Target')
identifier = w._article_details_of(target).identifier


def unrelated(d, owner, group):
    editor = w.new_tab()
    editor.insertPlainText('Unrelated text')
    w._set_article_details(editor, ArticleDetails(identifier=d).with_language('en'))
    editor._language_link = {'version': 1, 'groupId': group, 'owner': owner, 'versions': {'en': d}}
    w.tabs.tabBar().moveTab(w._editor_widget_index(editor), 0)
    return editor


same_d = unrelated(identifier, PK, 'another-group')
foreign_d = unrelated(identifier, OTHER_PK, 'foreign-group')
source_d = unrelated('group-source', PK, 'another-source-group')
event = signed(30023, [['d', identifier], ['published_at', '1700000000']], 'Target')
w._language_version_published(target, event)
result = {
    'target_updated': target._published_event['id'] == event['id'],
    'same_d_untouched': getattr(same_d, '_published_event', None) is None,
    'foreign_d_untouched': getattr(foreign_d, '_published_event', None) is None,
}
# A route/tab switch does not retarget the pair's source receipt.
w.tabs.setCurrentIndex(w._editor_widget_index(foreign_d))
source_body = w._publish_payload(source, 'markdown', keep_local=True)[0]
source_event = signed(30023, [['d', 'group-source'], ['title', 'Original'],
                             ['L', 'ISO-639-1'], ['l', 'de', 'ISO-639-1']], source_body)
w._language_version_published(target, source_event)
result['pair_source_updated'] = source._published_event['id'] == source_event['id']
result['unrelated_source_untouched'] = getattr(source_d, '_published_event', None) is None
result['target_receipt_still_target'] = target._published_event['id'] == event['id']
result['accepted_source_pin_kept'] = target._language_link['source']['eventId'] == source_event['id']
# A receipt signed by another owner cannot replace this linked target's state.
foreign_event = signed(30023, [['d', identifier]], 'Foreign target', sk=OTHER_SK)
w._language_version_published(target, foreign_event)
result['foreign_receipt_ignored'] = target._published_event['id'] == event['id']
# Optional legacy ownership is adopted on encrypted restore, not confused
# with a different explicit signing account.
from nostr.drafts import serialize_inner_event, parse_inner_event
for legacy_owner in (None, ''):
    local = {'version': 1, 'groupId': 'legacy-' + str(legacy_owner),
             'versions': {'en': 'legacy-' + str(legacy_owner)}}
    if legacy_owner is not None:
        local['owner'] = legacy_owner
    inner = {'kind': 30023, 'pubkey': PK, 'created_at': 1700000000,
             'content': 'Restored target', 'tags': [['d', local['versions']['en']]],
             'standup_multilingual': local}
    restored = w.new_tab()
    restored.insertPlainText('Restored target')
    restored._language_link = parse_inner_event(serialize_inner_event(inner))['standup_multilingual']
    w._set_article_details(restored, ArticleDetails(identifier=local['versions']['en']).with_language('en'))
    context = w._multilingual.context(restored)
    restored_event = signed(30023, [['d', local['versions']['en']]], 'Restored target')
    w._language_version_published(restored, restored_event)
    result['legacy_owner_' + str(legacy_owner)] = (context['owner'] == PK
        and restored._language_link['owner'] == PK
        and restored._language_link['published']['eventId'] == restored_event['id'])
# An ordinary publish is remembered, and changes nothing of the document
# (me68-red-a-5): its link name and date come back through the sheet.
ordinary = w.new_tab()
ordinary.insertPlainText('First published article')
w._set_article_details(ordinary, ArticleDetails(identifier='first-publication', title='First'))
ordinary.document().setModified(False)
before = w._article_details_of(ordinary)
first_event = signed(30023, [['d', 'first-publication'], ['published_at', '1700000000'],
                             ['alt', 'An article'], ['L', 'ISO-639-1'], ['l', 'en', 'ISO-639-1']],
                     'First published article')
w._language_version_published(ordinary, first_event)
result['first_receipt_updated'] = ordinary._published_event['id'] == first_event['id']
result['ordinary_publish_changes_nothing'] = (not ordinary.document().isModified()
                                              and w._article_details_of(ordinary) == before)
result['first_public_reference_known'] = w._multilingual._snapshot(ordinary, '')['public']
for editor in w._multilingual.editors():
    editor.document().setModified(False)
w.close()
print('RESULT ' + json.dumps(result))
'''


def test_receipts_follow_the_reviewed_group_owner_and_target_after_tab_switch(tmp_path):
    result = run_window_script(SCRIPT, tmp_path)
    assert all(result.values()), result


def test_late_focus_callback_ignores_a_destroyed_publish_scroll():
    import shiboken6
    from PySide6.QtWidgets import QLineEdit, QScrollArea
    from nostr.ui.publish_sheet import _FocusInView
    scroll = QScrollArea()
    follower = _FocusInView(scroll)
    field = QLineEdit()
    shiboken6.delete(scroll)
    follower._focus_moved(None, field)
    field.deleteLater()
