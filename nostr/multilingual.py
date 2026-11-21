"""Validated translation declarations; membership needs verified public evidence.

These markers are an application convention, not a Nostr translation NIP.
Parsing never treats a reference or credited person as proof of authority.
"""
from __future__ import annotations
import hashlib
import json
import re
from .content_language import language_from_tags, normalize_language, is_language_tag

PRIVATE_LINK_TAG = 'standup:multilingual'
REVISION_FORMAT = 2
_HEX = re.compile(r'^[0-9a-f]{64}$')


def article_address(value):
    if not isinstance(value, str) or len(value) > 1200:
        return ''
    parts = value.split(':', 2)
    return value if len(parts) == 3 and parts[0] == '30023' and _HEX.fullmatch(parts[1]) and parts[2] else ''


def is_translation_tag(tag):
    return isinstance(tag, (list, tuple)) and len(tag) > 3 and (
        (tag[0] == 'a' and tag[3] in ('original', 'source', 'translation'))
        or (tag[0] == 'e' and tag[3] in ('original', 'source')))


def translation_metadata(tags):
    roots, sources, pins, legacy, translations = [], [], [], [], []
    root_valid = source_valid = True
    for tag in tags or ():
        if not is_translation_tag(tag):
            continue
        if not all(isinstance(part, str) for part in tag):
            if tag[0] == 'a' and tag[3] in ('original', 'translation'): root_valid = False
            else: source_valid = False
            continue
        marker = tag[3]
        if tag[0] == 'a':
            address = article_address(tag[1])
            if marker in ('original', 'translation') and not address: root_valid = False
            if marker == 'source' and not address: source_valid = False
            {'original': roots, 'source': sources, 'translation': translations}[marker].append(address)
        else:
            valid = bool(_HEX.fullmatch(tag[1])) and (len(tag) < 5 or not tag[4] or bool(_HEX.fullmatch(tag[4])))
            (pins if marker == 'source' else legacy).append((tag[1], tag[4] if len(tag) > 4 else ''))
            source_valid &= valid
    roots = list(dict.fromkeys(roots)); sources = list(dict.fromkeys(sources)); pins = list(dict.fromkeys(pins)); legacy = list(dict.fromkeys(legacy))
    root_valid &= len(roots) <= 1
    root = roots[0] if root_valid and roots else ''
    source_valid &= len(sources) <= 1 and len(pins) <= 1
    source = sources[0] if len(sources) == 1 else ''
    pin = pins[0] if len(pins) == 1 else ('', '')
    if not sources and not pins and legacy:
        source_valid &= len(legacy) == 1 and bool(root)
        source = root
        pin = legacy[0] if len(legacy) == 1 else ('', '')
    elif bool(sources) != bool(pins):
        source_valid = False
    if source and pin[1] and source.split(':', 2)[1] != pin[1]: source_valid = False
    return {'root': root, 'source': source if source_valid else '',
            'source_event': pin[0] if source_valid else '',
            'source_pubkey': (source.split(':', 2)[1] if source else '') if source_valid else '',
            'translations': list(dict.fromkeys(t for t in translations if t)),
            'root_valid': bool(root_valid), 'source_valid': bool(source_valid),
            'valid': bool(root_valid and source_valid)}


def parse_relations(event):
    metadata = translation_metadata(event.get('tags', []) if isinstance(event, dict) else [])
    if isinstance(event, dict):
        identifier = next((t[1] for t in event.get('tags', []) if isinstance(t, (list, tuple)) and len(t) > 1 and t[0] == 'd'), '')
        address = article_address(f"{event.get('kind')}:{event.get('pubkey')}:{identifier}")
        if address and metadata['root'] == address:
            metadata.update(root='', root_valid=False, valid=False)
    return metadata


def translation_tags(metadata):
    tags = []
    root, source, event = metadata.get('root', ''), metadata.get('source', ''), metadata.get('source_event', '')
    if root:
        if not article_address(root): raise ValueError('Invalid root address')
        tags.append(['a', root, '', 'original'])
    if source or event:
        if not article_address(source) or not isinstance(event, str) or not _HEX.fullmatch(event):
            raise ValueError('A source needs its public address and signed revision')
        tags.extend([['a', source, '', 'source'], ['e', event, '', 'source', source.split(':', 2)[1]]])
    for address in dict.fromkeys(metadata.get('translations', [])):
        if not article_address(address): raise ValueError('Invalid translation address')
        tags.append(['a', address, '', 'translation'])
    return tags


def translatable_projection(event):
    tags = event.get('tags', [])
    def first(name):
        return next((t[1] for t in tags if isinstance(t, (list, tuple)) and len(t) > 1 and t[0] == name), '')
    return {'content': event.get('content', ''), 'image': first('image'), 'language': language_from_tags(tags),
            'summary': first('summary'), 'title': first('title')}


def projection_hash(event):
    value = json.dumps(translatable_projection(event), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def requires_multilingual(work):
    """Only an explicit link to another version, or a translator's credit, needs revision format 2. A language label
    alone, also a qualified one such as ``en-US``, is an ordinary article (contract version 2)."""
    return any(is_translation_tag(t) or (isinstance(t, (list, tuple)) and len(t) > 3 and t[0] == 'p' and t[3] == 'translator')
               for t in work.get('extra_tags', []))


def check_multilingual_work(work):
    if work.get('lang') and not normalize_language(work['lang']): raise ValueError('Invalid content language')
    tags = work.get('extra_tags', [])
    labels = [t for t in tags if is_language_tag(t)]
    if work.get('lang') and any(t[0] == 'l' for t in labels) and language_from_tags(labels) != normalize_language(work['lang']):
        raise ValueError('Content language contradicts carried labels')
    if any(t and t[0] == PRIVATE_LINK_TAG for t in tags): raise ValueError('Private source metadata cannot be submitted')
    if not translation_metadata(tags)['valid']: raise ValueError('Invalid translation provenance')
    for t in tags:
        if len(t) > 3 and t[0] == 'p' and t[3] == 'translator' and not _HEX.fullmatch(t[1]):
            raise ValueError('Invalid translator key')


def supports_revision_format(key, version=REVISION_FORMAT):
    rules = getattr(key, 'rules', None)
    if rules is None and isinstance(key, dict):
        content = key.get('content', key)
        rules = content.get('rules', {}) if isinstance(content, dict) else {}
    formats = rules.get('revision_formats', []) if isinstance(rules, dict) else []
    return version == 1 or (isinstance(formats, (list, tuple))
                            and all(type(v) is int and v > 0 for v in formats)
                            and version in formats)



def check_revision_work(work, version, *, error=ValueError):
    """Require preservation semantics before a revision may be mutated."""
    try:
        if version == 2:
            check_multilingual_work(work)
        elif requires_multilingual(work):
            raise ValueError('multilingual work requires revision format 2')
    except ValueError as exc:
        raise error(str(exc)) from None
