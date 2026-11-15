# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A picture's web address standing bare in the text, shown as the picture.

STANDUP's page shows a bare ``https?://`` address that points at a
picture as that picture, the same as ``![](address)``: alone on its line
a figure, several on lines of their own a group, in a sentence a picture
there (backend/htmlgen/md.py, bare_urls, classifying with
core/nostr_content.py). The editor keeps such an address as the link or
words it was typed as; the preview and the exports that carry pictures
(the page, PDF and print) show what the page shows.

"Bare" is as the file has it: the words markdown_writer writes as they
stand (bare_words). An address it writes ``<…>``, a link with words of
its own and code stay as they are there, as on the page. The rules
below are md.py's and nostr_content.py's at STANDUP 5aee088, as written
there.
"""

from __future__ import annotations

import re
from typing import List, Tuple
from urllib.parse import parse_qs, urlparse

from PySide6.QtGui import QTextCursor, QTextDocument, QTextImageFormat

import photo_group_find
from markdown_writer import bare_words

# md.py _URL_PATTERN (mistune's url plugin): a bare address, as far as it
# runs, without the punctuation a sentence puts after it.
_URL = re.compile(r'''https?:\/\/[^\s<]+[^<.,:;"')\]\s]''')
# nostr_content.py _TRAILING_PUNCT_RE: peeled off the address one by one.
_TRAILING_PUNCT = re.compile(r'[.,;:!?)\]]$')
# nostr_content.py _IMAGE_EXT_RE: a picture, by its path's ending.
_PICTURE_PATH = re.compile(r'\.(jpg|jpeg|png|webp|gif|avif|bmp)(?:\?|#|$)', re.IGNORECASE)
# nostr_content.py video_embed_url: the hosts whose pages it plays.
_YOUTUBE = ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be',
            'www.youtu.be', 'www.youtube-nocookie.com', 'youtube-nocookie.com')
_VIMEO = ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com')
_ODYSEE = ('odysee.com', 'www.odysee.com')


def _peeled(token: str) -> str:
    """``token`` without the punctuation a sentence puts after it
    (nostr_content.parse_content)."""
    while len(token) > 8 and _TRAILING_PUNCT.search(token):
        token = token[:-1]
    return token


def _youtube_id(host: str, path: List[str], query: str) -> str:
    if not path:
        return ''
    if host.endswith('youtu.be'):
        return path[0]
    if path[0] == 'watch':
        return parse_qs(query).get('v', [''])[0]
    if path[0] in ('shorts', 'embed', 'live') and len(path) > 1:
        return path[1]
    return ''


def _plays_as_video(address: str) -> bool:
    """Whether STANDUP plays ``address`` as a video (video_embed_url gives
    an embed for it), which comes before a picture."""
    parsed = urlparse(address)
    if parsed.scheme not in ('http', 'https') or parsed.username or parsed.password:
        return False
    host = (parsed.hostname or '').lower()
    path = [part for part in parsed.path.split('/') if part]
    if host in _YOUTUBE:
        return bool(re.fullmatch(r'[A-Za-z0-9_-]{11}', _youtube_id(host, path, parsed.query)))
    if host in _VIMEO and path:
        return bool(re.fullmatch(r'\d+', path[1] if path[0] == 'video' and len(path) > 1
                                 else path[0]))
    if host in _ODYSEE:
        content = re.sub(r'^/(?:\$|%24)/embed/', '/', parsed.path, flags=re.I).lstrip('/')
        return not (not content or content.startswith('$')
                    or (content.startswith('@') and '/' not in content))
    return False


def is_picture(address: str) -> bool:
    """Whether STANDUP shows the bare ``address`` as a picture
    (nostr_content.media_kind_for_url is 'image')."""
    if not re.match(r'^https?://', address, re.I):
        return False
    try:
        return not _plays_as_video(address) and bool(_PICTURE_PATH.search(urlparse(address).path))
    except ValueError:
        return False


def pictures_in(text: str, start: int, end: int) -> List[Tuple[int, str]]:
    """The pictures of the bare addresses that start in ``text[start:end]``
    (words written as they stand), each where it starts and its address.
    An address runs on as far as ``text`` does, as md.py's does in the
    file: one that runs past ``end`` (into words written around these) is
    none."""
    out: List[Tuple[int, str]] = []
    at = start
    while True:
        found = _URL.search(text, at, end)
        if found is None:
            return out
        token = _URL.match(text, found.start()).group(0)
        address = _peeled(token)
        if found.start() + len(address) <= end and is_picture(address):
            out.append((found.start(), address))
        at = found.start() + len(token)


def _bare_pictures(doc: QTextDocument) -> List[Tuple[int, str]]:
    found: List[Tuple[int, str]] = []
    block = doc.begin()
    while block.isValid():
        text = block.text()
        for start, end in bare_words(block):
            found += [(block.position() + at, address)
                      for at, address in pictures_in(text, start, end)]
        block = block.next()
    return found


def show(doc: QTextDocument) -> int:
    """Show each picture address standing bare in ``doc`` (a preview's or
    an export's own copy, never the editor's) as its picture, with no
    description, and the pictures that then follow each other as their
    group, as when a file is read; how many."""
    found = _bare_pictures(doc)
    if not found:
        return 0
    cursor = QTextCursor(doc)
    cursor.beginEditBlock()
    for position, address in reversed(found):
        cursor.setPosition(position)
        cursor.setPosition(position + len(address), QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        picture = QTextImageFormat()
        picture.setName(address)
        cursor.insertImage(picture)
    cursor.endEditBlock()
    photo_group_find.fold(doc)
    return len(found)

