# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private language linkage inside encrypted draft plaintext, never event tags.

Supported previous MyEditor and STANDUP builders whitelist ordinary event
fields. They may drop this optional state, but do not republish it as tags.
A persisted flag or signed snapshot never establishes public availability.
"""
from __future__ import annotations
import json
import re

from .content_language import normalize_language
from .multilingual import PRIVATE_LINK_TAG

PRIVATE_DRAFT_FIELD = 'standup_multilingual'
MAX_PRIVATE_DRAFT_BYTES = 2 * 1024 * 1024
_HEX = re.compile(r'^[0-9a-f]{64}$')


def private_multilingual(value):
    """A bounded independently owned record, preserving unknown local fields.

    `view`, file paths, source snapshots and accepted publication information
    belong to private persistence. Returning them here never authorizes public
    tags; the publication path separately verifies signed public evidence.
    """
    if not isinstance(value, dict) or type(value.get('version', 1)) is not int or value.get('version', 1) != 1:
        return None
    if not isinstance(value.get('groupId'), str) or not value['groupId']:
        return None
    owner = value.get('owner', '')
    if not isinstance(owner, str) or owner and not _HEX.fullmatch(owner):
        return None
    versions = value.get('versions')
    if not isinstance(versions, dict) or not all(
            isinstance(code, str) and normalize_language(code) == code
            and isinstance(identifier, str) and identifier for code, identifier in versions.items()):
        return None
    source = value.get('source')
    if source is not None and (not isinstance(source, dict) or not all(
            isinstance(source.get(key, ''), str)
            for key in ('dTag', 'language', 'title', 'content', 'eventId', 'address'))):
        return None
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        if len(raw.encode('utf-8')) > MAX_PRIVATE_DRAFT_BYTES:
            return None
        return json.loads(raw)
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return None


def without_private_tags(tags):
    """Discard the unreleased candidate's unsafe private-tag representation."""
    return [tag for tag in tags if not (isinstance(tag, (list, tuple)) and tag and tag[0] == PRIVATE_LINK_TAG)]
