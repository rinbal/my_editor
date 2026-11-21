# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private draft extension survives supported new writers and cannot leak via old ones."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from nostr.drafts import build_inner_event, parse_inner_event, serialize_inner_event
from nostr.private_draft import PRIVATE_DRAFT_FIELD, MAX_PRIVATE_DRAFT_BYTES, private_multilingual

OWNER='a'*64
SECRET='PRIVATE SOURCE BODY MUST NEVER BECOME PUBLIC'
LINK={'version':1,'groupId':'a-group','owner':OWNER,'versions':{'de':'original','zh-Hant':'target'},
      'source':{'dTag':'original','language':'de','title':'Original','content':SECRET,'eventId':'','address':''},
      'view':{'reference':True,'ratio':0.33},'documentId':'target','documents':{'original':'/private/local/original.md'},
      'published':{'eventId':'1'*64,'address':'30023:'+OWNER+':target'}}

def inner():
    event=build_inner_event(kind=30023,content='Translated public body',pubkey_hex=OWNER,
                            created_at=100,tags=[['d','target'],['title','Translated'],['l','zh-Hant','lang']])
    event[PRIVATE_DRAFT_FIELD]=deepcopy(LINK)
    return event


def test_private_extension_roundtrips_separately_from_tags_and_is_independent():
    value=inner(); serialized=serialize_inner_event(value); parsed=parse_inner_event(serialized)
    assert parsed[PRIVATE_DRAFT_FIELD]==LINK
    assert SECRET not in json.dumps(parsed['tags'])
    assert 'standup:multilingual' not in serialized
    assert 'sig' not in parsed and 'id' not in parsed
    parsed[PRIVATE_DRAFT_FIELD]['source']['content']='changed'
    assert value[PRIVATE_DRAFT_FIELD]['source']['content']==SECRET


def test_old_known_reader_and_public_builders_cannot_publish_private_extension():
    import nostr.drafts as drafts
    import nostr.article_details as details
    import nostr.publisher as publisher
    snapshots=json.loads(Path(__file__).with_name('fixtures').joinpath('private-draft-old-builders.json').read_text())
    scopes={name:dict(vars(module)) for name,module in [('drafts',drafts),('details',details),('publisher',publisher)]}
    for name,source in snapshots['sources'].items():
        exec(compile(source,'upstream-a88c403a-'+name,'exec'),scopes[name])
    # Actual old parser function's whitelist discards the private root key.
    parsed=scopes['drafts']['parse_inner_event'](serialize_inner_event(inner()))
    assert PRIVATE_DRAFT_FIELD not in parsed and SECRET not in json.dumps(parsed)
    old_details=scopes['details']['ArticleDetails'].from_tags(parsed['tags'],signer=OWNER)
    old_article=scopes['publisher']['build_article'](parsed['content'],OWNER,'target',title=old_details.title,
        extra_tags=old_details.published_tags(parsed['content'],author=(OWNER,'')))
    assert SECRET not in json.dumps(old_article)
    assert PRIVATE_DRAFT_FIELD not in old_article
    assert 'standup:multilingual' not in json.dumps(old_article)
    # Negative control proves these actual older builders leak the rejected tag design.
    unsafe=scopes['details']['ArticleDetails'].from_tags([*parsed['tags'],['standup:multilingual',json.dumps(LINK)]],signer=OWNER)
    leaked=scopes['publisher']['build_article'](parsed['content'],OWNER,'target',extra_tags=unsafe.published_tags(parsed['content']))
    assert SECRET in json.dumps(leaked)


def test_unreleased_private_tag_never_survives_a_new_parse_save_or_store():
    value=inner();value['tags'].append(['standup:multilingual',json.dumps(LINK)])
    parsed=parse_inner_event(json.dumps(value))
    assert all(t[0]!='standup:multilingual' for t in parsed['tags'])
    assert 'standup:multilingual' not in serialize_inner_event(value)


@pytest.mark.parametrize('value',[{'version':2}, {'version':True}, {'groupId':'g','versions':{'xx':'target'}},
    {**LINK,'owner':'not-a-key'},{**LINK,'source':{'content':[]}}, {**LINK,'source':{'content':'x'*MAX_PRIVATE_DRAFT_BYTES}}])
def test_invalid_private_extension_never_changes_standard_event_fields(value):
    original=inner();original[PRIVATE_DRAFT_FIELD]=value
    parsed=parse_inner_event(serialize_inner_event(original))
    assert PRIVATE_DRAFT_FIELD not in parsed
    assert parsed['content']=='Translated public body' and parsed['tags']==original['tags']


def test_draft_store_retains_private_extension_separately_and_clears_on_ordinary_replacement():
    from PySide6.QtCore import QCoreApplication
    from nostr.draft_store import DraftStore
    app=QCoreApplication.instance() or QCoreApplication([])
    store=DraftStore();store.bind_profile(OWNER)
    value=inner();store.upsert_from_inner(identifier='target',inner=value,event_id='old',created_at=100,expiration=None)
    record=store.get('target')
    assert record.standup_multilingual==LINK
    assert SECRET not in json.dumps(record.inner_tags)
    value[PRIVATE_DRAFT_FIELD]['source']['content']='changed input'
    assert record.standup_multilingual['source']['content']==SECRET
    store.set_decrypted('target',inner=build_inner_event(kind=30023,content='Ordinary',pubkey_hex=OWNER,tags=[['d','target']]),source_event_id='old')
    assert store.get('target').standup_multilingual is None
    store.bind_profile('b'*64)
    assert store.get('target') is None
