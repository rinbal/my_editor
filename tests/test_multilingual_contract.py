"""Shared wire vectors plus draft/file/editorial preservation and old-reader refusal."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
import front_matter
from nostr.article_details import ArticleDetails, front_matter_with, from_front_matter
from nostr.content_language import normalize_language, language_from_tags, language_tags, search_languages
from nostr.multilingual import translation_metadata, projection_hash, translatable_projection
from nostr.publisher import build_article
from nostr.standup import formats, publishing
V = json.loads(Path(__file__).with_name('fixtures').joinpath('multilingual-vectors.json').read_text())

@pytest.mark.parametrize('case', V['languages'], ids=lambda c: str(c['input']))
def test_registered_language_vectors(case):
    assert normalize_language(case['input']) == case['expected']

@pytest.mark.parametrize('case', V['labels'])
def test_label_resolution_vectors(case):
    assert language_from_tags(case['tags']) == case['language']
    assert language_from_tags(list(reversed(case['tags']))) == case['language']

@pytest.mark.parametrize('case', V['relations'], ids=lambda c: c['name'])
def test_declaration_vectors(case):
    assert translation_metadata(case['tags']) == case['expected']

def test_content_projection_ignores_link_only_edits_and_keeps_unicode():
    case = V['projection']; event = deepcopy(case['event'])
    assert translatable_projection(event) == case['expected']
    assert projection_hash(event) == case['sha256']
    event['tags'].append(['a', '30023:' + 'a' * 64 + ':another', '', 'translation'])
    event['created_at'] += 10
    assert projection_hash(event) == case['sha256']
    event['content'] += ' more'
    assert projection_hash(event) != case['sha256']

@pytest.mark.parametrize('query,code', [('ไทย','th'),('Laos','lo'),('ລາວ','lo'),('ខ្មែរ','km'),('Japan','ja'),('日本語','ja'),('Hmong','hmn')])
def test_full_catalogue_native_alias_search(query, code):
    assert search_languages(query, limit=1)[0]['code'] == code

@pytest.mark.parametrize('code', ['th', 'lo', 'km', 'ja', 'hmn', 'zh-Hant-TW'])
def test_language_and_provenance_survive_file_draft_and_publication(code):
    revision = V['editorial']['revision']; tags = revision['work']['extra_tags']
    original = ArticleDetails.from_tags([['d','version'],*tags]).with_language(code)
    assert ArticleDetails.from_tags(original.draft_tags('Text')).language == code
    front = front_matter_with(original, None)
    reparsed, _ = front_matter.split(front_matter.join(front, 'Text'))
    restored = from_front_matter(reparsed)
    assert restored.language == code
    assert translation_metadata(restored.tags()) == translation_metadata(tags)
    assert ['p', V['editorial']['revision']['work']['authors'][0]['pubkey'], '', 'translator'] in restored.published_tags('Text')
    event = build_article('Text', 'a'*64, 'version', extra_tags=restored.published_tags('Text'))
    assert language_from_tags(event['tags']) == code

def test_private_link_never_enters_public_details_or_builder():
    tag = ['standup:multilingual', '{"private":"secret draft"}']
    details = ArticleDetails.from_tags([tag])
    assert tag not in details.tags()
    event = build_article('Text', 'a'*64, 'version', extra_tags=[tag])
    assert tag not in event['tags']

def test_complete_publication_template():
    case = V['editorial']; revision = case['revision']
    assert formats.check_revision(revision) is revision
    actual = publishing.published_template(revision, writer=case['writer'], d=revision['work']['d'], media_urls={}, published_at=100, created_at=100)
    assert actual == case['published']['myeditor']

def test_complete_correction_advances_only_accepted_provenance():
    case=V['editorial']
    actual=publishing.corrected_template(case['correction'], case['before'], writer=case['writer'], media_urls={}, created_at=102)
    assert actual == case['corrected']['myeditor']
    assert ['p',V['editorial']['revision']['work']['authors'][0]['pubkey'],'','translator'] in actual['tags']
    assert ['zap',case['writer'],'','100'] in actual['tags']

def test_old_reader_version_guard_refuses_new_revision():
    # Exact check_revision function from the recorded upstream a88c403a.
    source=Path(__file__).with_name('fixtures').joinpath('editorial-v1-check_revision.py.txt').read_text()
    scope=dict(vars(formats));exec(compile(source,'upstream-a88c403a-check_revision','exec'),scope)
    with pytest.raises(formats.FormatError, match='version 1'):
        scope['check_revision'](V['editorial']['revision'])

def test_ordinary_v1_still_opens_and_multilingual_cannot_disguise_itself_as_v1():
    value=deepcopy(V['editorial']['revision']); value['v']=1
    with pytest.raises(formats.FormatError, match='format 2'):formats.check_revision(value)
    value['work']['extra_tags']=[]
    assert formats.check_revision(value) is value

def test_v2_encryption_matches_web_bytes_and_opens_without_loss():
    from nostr.standup.crypto import seal_revision, _open_revision
    case=V['editorial']; vector=case['envelope']
    sealed=seal_revision(case['revision'],bytes.fromhex(vector['s_sub']),nonce=bytes.fromhex(vector['nonce']))
    assert sealed.envelope_b64 == vector['envelope']
    assert sealed.envelope_sha256 == vector['envelope_sha256']
    assert sealed.revision_hash == vector['revision_hash']
    assert _open_revision(vector['envelope'],bytes.fromhex(vector['s_sub']),submission_id=case['revision']['submission_id'],revision=1,revision_hash=vector['revision_hash']) == case['revision']

def test_recovery_never_treats_an_older_source_pin_as_completed():
    case=V['editorial']
    assert ('reference','') in publishing.correction_changes(case['correction'],case['published']['myeditor'])

def test_contract_pins_catalogue_and_shared_vectors():
    import hashlib
    import nostr
    package=Path(nostr.__file__).parent
    contract=json.loads((package/'multilingual_contract.json').read_text())
    assert hashlib.sha256((package/'language_catalog.json').read_bytes()).hexdigest()==contract['catalog_sha256']
    assert hashlib.sha256(Path(__file__).with_name('fixtures').joinpath('multilingual-vectors.json').read_bytes()).hexdigest()==contract['vectors_sha256']

def test_source_pin_correction_cannot_smuggle_a_translator_credit_change():
    case=V['editorial'];revision=deepcopy(case['correction'])
    revision['work']['extra_tags']=[t for t in revision['work']['extra_tags'] if not (t[0]=='p' and t[3]=='translator')]
    with pytest.raises(publishing.NotPublishable, match='translator credit'):
        publishing.corrected_template(revision,case['before'],writer=case['writer'],media_urls={},created_at=102)

@pytest.mark.parametrize('advertised',[None,'2',{'2':True},[True,2],[2.0],[2,'1']])
def test_malformed_capability_cannot_enable_multilingual_submission(advertised):
    from nostr.multilingual import supports_revision_format
    assert not supports_revision_format({'content':{'rules':{'revision_formats':advertised}}})

def test_existing_qualified_versions_are_available_without_typing_codes():
    from nostr.content_language import language_name
    codes=['zh-Hant','zh-Hans','pt-BR','en']
    rows=search_languages('',recent=codes,limit=4)
    assert [r['code'] for r in rows]==codes
    assert len({r['native_name'] for r in rows})==4
    assert language_name('zh-Hant','native')!=language_name('zh-Hans','native')
    assert search_languages('pt_BR',recent=codes,limit=1)[0]['code']=='pt-BR'
