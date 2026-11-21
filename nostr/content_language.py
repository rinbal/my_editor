"""Pinned offline writing languages and validated NIP-32 language labels.

Language identity is independent of interface translations and detection. The
registry is vendored; a document is never sent to a service to name its language.
"""
from __future__ import annotations
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

_CATALOG = json.loads(Path(__file__).with_name('language_catalog.json').read_text(encoding='utf-8'))
_NAMES = dict(_CATALOG['languages'])
_VALIDATION = _CATALOG['validation']
NAMESPACE = 'ISO-639-1'
LANGUAGE_NAMESPACES = frozenset((NAMESPACE, 'lang'))


def normalize_language(value):
    """Canonical registered language/script/region/variant, or an empty string.

    Registered aliases are canonicalized. A syntactically plausible but
    unregistered subtag is never accepted as a language selection.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return ''
    text = value.strip().replace('_', '-').lower()
    text = _VALIDATION['grandfathered'].get(text, text).lower()
    parts = text.split('-')
    primary = _VALIDATION['aliases'].get(parts[0], parts[0]).lower()
    if primary not in _NAMES:
        return ''
    out = [primary]
    i = 1
    if i < len(parts) and parts[i] in _VALIDATION['extlang']:
        entry = _VALIDATION['extlang'][parts[i]]
        if primary not in [p.lower() for p in entry['prefixes']]:
            return ''
        out = [entry['preferred'].lower()]
        i += 1
    if i < len(parts) and parts[i] in _VALIDATION['script']:
        out.append(_VALIDATION['script'][parts[i]].title())
        i += 1
    if i < len(parts) and parts[i] in _VALIDATION['region']:
        out.append(_VALIDATION['region'][parts[i]].upper())
        i += 1
    used = set()
    while i < len(parts):
        part = parts[i]
        entry = _VALIDATION['variant'].get(part)
        if not entry or part in used:
            return ''
        prefix = '-'.join(out).lower()
        if entry['prefixes'] and not any(prefix == p.lower() or prefix.startswith(p.lower() + '-')
                                         for p in entry['prefixes']):
            return ''
        out.append(entry['preferred'].lower())
        used.add(part)
        i += 1
    return '-'.join(out)


def is_language_tag(tag):
    return isinstance(tag, (list, tuple)) and bool(tag) and (
        (tag[0] == 'L' and len(tag) > 1 and tag[1] in LANGUAGE_NAMESPACES)
        or (tag[0] == 'l' and len(tag) > 2 and tag[2] in LANGUAGE_NAMESPACES))


def has_language_declaration(tags):
    """An explicit language label, including one that cannot be resolved safely."""
    return any(isinstance(tag, (list, tuple)) and len(tag) > 2 and tag[0] == 'l'
               and tag[2] in LANGUAGE_NAMESPACES for tag in tags or ())


def language_from_tags(tags):
    """Resolve labels without tag-order dependence; conflicting values are unknown."""
    found = {'ISO-639-1': set(), 'lang': set()}
    for tag in tags or ():
        if not isinstance(tag, (list, tuple)) or len(tag) < 3 or tag[0] != 'l' or tag[2] not in found:
            continue
        code = normalize_language(tag[1])
        if code and (tag[2] != NAMESPACE or len(code.split('-')[0]) == 2):
            found[tag[2]].add(code)
    if any(len(values) > 1 for values in found.values()):
        return ''
    qualified = next(iter(found['lang']), '')
    legacy = next(iter(found[NAMESPACE]), '')
    return '' if qualified and legacy and qualified != legacy and not (len(legacy) == 2 and qualified.split('-')[0] == legacy) else qualified or legacy


def language_tags(code):
    """The labels for ``code``. A qualified code with a two-letter base also carries that base in ISO-639-1, so
    readers that know only ISO-639-1 keep the language (``language_from_tags`` reads the pair as the qualified code)."""
    language = normalize_language(code)
    if not language:
        return []
    base = language.split('-')[0]
    iso = [['L', NAMESPACE], ['l', base, NAMESPACE]] if len(base) == 2 else []
    return iso if language == base and iso else [*iso, ['L', 'lang'], ['l', language, 'lang']]


_LOCALIZED = _CATALOG['localized_names']['locales']


def language_name(code, locale='en'):
    """A language's name in an interface ``locale`` from the shared catalogue (CLDR), or ``'native'``."""
    language = normalize_language(code)
    if not language:
        return ''
    base = language.split('-')[0]
    if locale == 'native':
        name = _CATALOG['native_names'].get(base)
    else:
        name = _LOCALIZED.get(str(locale).split('-')[0].lower(), {}).get(base)
    return (name or _NAMES.get(base, [base])[0]) + (f' ({language})' if '-' in language else '')


def _search_text(value):
    return re.sub('[\u0300-\u036f]', '', unicodedata.normalize('NFKD', value)).lower().strip()


@lru_cache(maxsize=1)
def _search_entries():
    entries = []
    for code, names in _CATALOG['languages']:
        native = _CATALOG['native_names'].get(code, names[0])
        localized = [names_in[code] for names_in in _LOCALIZED.values() if code in names_in]
        labels = [code, native, *names, *localized, *_CATALOG['search_aliases'].get(code, [])]
        entries.append(({'code': code, 'name': names[0], 'native_name': native}, tuple(map(_search_text, labels))))
    return entries


def search_languages(query='', recent=(), limit=40, offset=0, locale='en'):
    """Bounded catalogue search, exact names before prefix/substring matches; ``name`` is in ``locale``."""
    needle = _search_text(normalize_language(query) or query)
    recent = list(dict.fromkeys(normalize_language(c) for c in recent if normalize_language(c)))
    ranked = []
    entries = list(_search_entries())
    for code in recent:
        if '-' in code:
            entry = {'code': code, 'name': language_name(code), 'native_name': language_name(code, 'native')}
            entries.append((entry, tuple(_search_text(v) for v in (code, entry['name'], entry['native_name']))))
    for index, (entry, labels) in enumerate(entries):
        entry = {**entry, 'name': language_name(entry['code'], locale)}
        rank = 0 if needle in labels else 1 if any(s.startswith(needle) for s in labels) else 2
        if needle and rank == 2 and not any(needle in label for label in labels):
            continue
        priority = recent.index(entry['code']) if entry['code'] in recent else len(recent)
        ranked.append((rank if needle else priority, index, entry))
    qualified = normalize_language(query)
    if qualified and '-' in qualified and qualified not in recent:
        ranked.insert(0, (-1, -1, {'code': qualified, 'name': language_name(qualified, locale),
                                  'native_name': language_name(qualified, 'native')}))
    ranked.sort(key=lambda item: item[:2])
    return [entry for _, _, entry in ranked[max(0, offset):max(0, offset) + min(max(1, limit), 200)]]
