# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end publishing: resolve relays → sign via signer → publish.

One generic ``PublishJob`` drives the pipeline for any unsigned event
(short notes, articles, anything else later). Concrete event builders
live alongside it as pure functions so callers stay declarative.

Shape of the flow:

  1. Two things start together: the relay directory works out where the
     event goes (the author's write relays, then the read relays of
     everyone it mentions, NIP-65), and the signer is asked to sign it.
     The user typically has to approve on their phone, which is plenty
     of time for the lookups to land.
  2. Once both are in, publish the signed event with eager-first-accept
     semantics.
  3. Hand the author's relay list to the mentioned people's relays that
     took the event, so readers there can find the author's other notes.
  4. Emit ``completed(results)`` with per-relay outcomes.

Drafts are private and go where every device of the author reads them
back (RelayDirectory.private_relays): reading asks every relay writing
goes to.

The builders also attach NIP-92 ``imeta`` tags for media, from records
the caller hands them. One rule governs every field: describe only media
this app itself resolved, and never fabricate a value for a URL nobody
here fetched. A foreign image passes through the content untouched and
gets no tag at all, because a wrong ``x`` tells every other client to
reject the blob it just downloaded, which is worse than an absent one.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QObject, Signal

import media_files
import url_safety
from i18n import _, ngettext

from . import CLIENT_NAME
from .article_details import AUTHOR_MARKER
from .bech32 import bech32_decode, decode_npub, decode_nprofile
from .blossom.hashes import blob_url, hash_from_url
from .bunker import BunkerClient, BunkerSessionPool
from .drafts import (
    DRAFT_WRAP_KIND,
    SUPPORTED_INNER_KINDS,
    build_draft_wrap,
    build_tombstone_wrap,
    serialize_inner_event,
)
from .draft_deletions import build_deletion_request
from .events import build_event, verify_event
from .outbox import RelayDirectory, ask_draft_relays, normalize_relay_url
from . import refs
from .outbox import defaults as outbox_defaults
from .outbox.lookup import Lookup, fetch_addressable
from .outbox.policy import dedupe_relays, is_newer
from .profiles import Profile
from .publish_pipeline import Mention, PublishPipeline, PublishResult
from .signer_limits import size_problem
from .relay import RelayPool


# Max length we'll surface from a signer-provided error string. Defensive
# truncation against signers that echo plaintext back in error messages
# (we've not observed it in practice but the contract isn't ours).
_MAX_REASON_CHARS: int = 200


def _safe_reason(reason: str) -> str:
    """Clip a signer-provided failure reason to ``_MAX_REASON_CHARS``.

    Keeps log lines bounded and protects against an over-talkative
    signer echoing the request payload into the error text.
    """
    if not reason:
        return _("unknown error")
    if len(reason) <= _MAX_REASON_CHARS:
        return reason
    return reason[:_MAX_REASON_CHARS - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Mention handling                                                            #
# --------------------------------------------------------------------------- #

# nostr:npub1… or nostr:nprofile1…  (NIP-21 URI form, NIP-19 bech32 body)
_NOSTR_URI_RE = re.compile(
    r"nostr:(n(?:pub|profile)1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]+)"
)


def extract_inline_mentions(content: str, *, markdown: bool = False) -> List[Mention]:
    """Find ``nostr:npub|nprofile`` URIs in ``content`` and decode them.

    Returns ``(pubkey_hex, relay_hint)`` tuples in document order; any URI
    that fails bech32 decoding is silently skipped (e.g. a near-miss that
    looks like a URI but isn't valid bech32). In ``markdown`` (an
    article), a URI in code is shown as written and mentions nobody, as a
    quote there quotes nothing (refs.find_references).
    """
    found: List[Mention] = []

    def collect(text: str) -> str:
        for match in _NOSTR_URI_RE.finditer(text):
            bech = match.group(1)
            try:
                if bech.startswith("npub1"):
                    pk = decode_npub(bech)
                    hint = ""
                else:  # nprofile1
                    pk, relays = decode_nprofile(bech)
                    hint = relays[0] if relays else ""
            except ValueError:
                continue
            found.append((pk, hint))
        return text

    if markdown:
        refs.outside_code(content, collect)
    else:
        collect(content)
    return found


def _mention_tags(content: str, *, markdown: bool) -> List[List[str]]:
    """A ``p`` tag for each person ``content`` mentions, once each, in the
    order of the text, with the relay hint written first for them."""
    seen: set = set()
    p_tags: List[List[str]] = []
    for pk, hint in extract_inline_mentions(content, markdown=markdown):
        if pk in seen:
            continue
        seen.add(pk)
        p_tags.append(["p", pk] + ([hint] if hint else []))
    return p_tags


# --------------------------------------------------------------------------- #
# Media attachments (NIP-92 imeta)                                             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PublishedMedia:
    """One image this app resolved into a URL it wrote into the content.

    Every field is something this process measured or a server it
    uploaded to confirmed. Nothing here may be inferred from a URL, and
    an unknown value stays at its default so the builder can leave the
    field out rather than guess it.
    """

    url: str
    sha256: str = ""
    mime: str = ""
    width: int = 0
    height: int = 0
    alt: str = ""
    size: int = 0
    servers: Tuple[str, ...] = ()


# One event carries at most this many imeta tags. The images stay in the
# content either way; only the metadata is capped.
_MAX_IMETA_TAGS = 100

# Alt text is prose and lands in someone else's UI, so it is clipped
# rather than trusted to be short.
_MAX_ALT_CHARS = 1000

# What the asset layer records when the bytes did not sniff to a known
# image type. Emitting it would claim a measurement nobody made, so the
# ``m`` field is dropped instead and readers can sniff for themselves.
_UNKNOWN_MIME = "application/octet-stream"

_WHITESPACE_RUN_RE = re.compile(r"\s+")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _is_injection_free(value: str) -> bool:
    """Whether ``value`` can be an imeta field value unchanged.

    imeta entries are space-delimited key/value pairs inside a variadic
    tag, so whitespace in a URL forges an extra field and a newline in
    any value forges an extra entry, or an extra tag, inside an event
    the user is about to sign. Both are refused at the source.
    """
    if not isinstance(value, str) or not value:
        return False
    return not _WHITESPACE_RUN_RE.search(value) and not _CONTROL_CHAR_RE.search(value)


def _clean_alt(value: str) -> str:
    """Alt text flattened onto one line and clipped.

    Alt is the one field where spaces are legitimate, so it is repaired
    instead of refused: control characters and newlines become spaces,
    runs collapse, and the result cannot introduce a second entry.
    """
    if not isinstance(value, str) or not value:
        return ""
    flattened = _CONTROL_CHAR_RE.sub(" ", value)
    return _WHITESPACE_RUN_RE.sub(" ", flattened).strip()[:_MAX_ALT_CHARS].strip()


def _fallbacks_for(record: PublishedMedia) -> List[str]:
    """Sibling addresses for one blob, from confirmed servers only.

    ``servers`` holds the origins that answered with this blob, and
    BUD-01 serves every endpoint from the root of the domain, so
    ``<origin>/<sha256>`` is an address this app can stand behind even
    though no server ever handed it that exact string. A configured but
    unconfirmed server never appears: a fallback nobody verified is a
    fabricated one, and it sends readers to a 404.
    """
    if not record.sha256:
        return []
    extension = _blob_extension(record)
    out: List[str] = []
    for origin in record.servers:
        candidate = blob_url(origin, record.sha256) + extension
        if candidate == record.url or url_safety.same_origin(candidate, record.url):
            continue
        if candidate in out or not _is_injection_free(candidate):
            continue
        if not url_safety.is_safe_media_url(candidate):
            continue
        out.append(candidate)
    return out


def _blob_extension(record: PublishedMedia) -> str:
    """The file extension a fallback address keeps: the one the primary
    address gives the blob (``<sha256>.png``), else the one its type has.
    BUD-01: a server MUST accept ``/<sha256>`` with an optional file
    extension, and a reader tells a picture from a sound by it before
    anything loads."""
    tail = record.url.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    name, dot, extension = tail.partition(".")
    if name.lower() == record.sha256.lower() and dot and extension.isalnum():
        return "." + extension
    return media_files.extension_for(record.mime)


# A web address in published text.
_MEDIA_ADDRESS = re.compile(r"https?://[^\s<>()\[\]\"']+")


def library_audio_video_in(files, content: str) -> List[PublishedMedia]:
    """The audio and video of the account's media library (``files``,
    sha256 -> MediaFile) that ``content`` holds, at the address written
    there, described from what the media servers confirmed when it was
    uploaded (type, hash, size): found by the hash its address carries
    (BUD-03, hash_from_url)."""
    found: List[PublishedMedia] = []
    for url in _MEDIA_ADDRESS.findall(content or ""):
        sha = hash_from_url(url)
        media = files.get(sha) if sha else None
        if media is None or not media.mime_type.startswith(("audio/", "video/")):
            continue
        servers = tuple(entry.get("server", "") for entry in media.urls if entry.get("server"))
        found.append(PublishedMedia(url=url, sha256=media.hash, mime=media.mime_type,
                                    size=media.size, servers=servers))
    return found


def build_imeta_tags(
    media: Sequence[PublishedMedia], content: str
) -> List[List[str]]:
    """NIP-92 ``imeta`` tags for the media this app put into ``content``.

    ``media`` comes from the walk that serialized the document, which is
    the only place that knows which URL was written for which image.
    Rediscovering the URLs with a regex over the finished content was
    rejected: it would find third-party addresses this app never
    fetched, and URLs the user typed as prose, and describing either one
    means guessing metadata nobody measured.

    Field order is fixed so the same document yields the same event id
    twice running. A record is dropped whole when its ``url`` is unsafe,
    is absent from the final content, or when no other field is known,
    since NIP-92 requires ``url`` plus at least one more.
    """
    tags: List[List[str]] = []
    described: set = set()
    for record in media or ():
        if len(tags) >= _MAX_IMETA_TAGS:
            break
        url = (record.url or "").strip()
        if url in described or not _is_injection_free(url):
            continue
        if not url_safety.is_safe_media_url(url):
            continue
        # NIP-92: each tag SHOULD match a URL in the event content: the
        # address itself, not one that only holds it (in its query, or as
        # the start of a longer one). A note's text can still be changed
        # in its publish sheet, so a picture described here may be gone.
        if not refs.address_in(url, content):
            continue

        entries: List[str] = []
        mime = (record.mime or "").strip().lower()
        if mime and mime != _UNKNOWN_MIME and _is_injection_free(mime):
            entries.append(f"m {mime}")
        sha = (record.sha256 or "").strip().lower()
        if sha and _is_injection_free(sha):
            entries.append(f"x {sha}")
        if record.width > 0 and record.height > 0:
            entries.append(f"dim {record.width}x{record.height}")
        alt = _clean_alt(record.alt)
        if alt:
            entries.append(f"alt {alt}")
        if record.size > 0:
            entries.append(f"size {record.size}")
        entries.extend(f"fallback {u}" for u in _fallbacks_for(record))
        if not entries:
            continue

        described.add(url)
        tags.append(["imeta", f"url {url}"] + entries)
    return tags


def _is_publishable_cover(url: str) -> bool:
    """Whether a NIP-23 cover address may be signed into an event.

    An imported article's cover is lifted straight out of untrusted feed
    HTML, so it arrives as anything: ``javascript:``, ``data:text/html``,
    ``file:///``, a protocol-relative address or a bare path.

    The mirror-source policy is the right gate rather than the media
    policy. Plenty of real blogs serve covers over plain http and
    refusing those would silently delete a legitimate image, while
    ``javascript:``, ``data:``, ``file:``, userinfo and private address
    literals are all refused. A refusal drops the tag and keeps the
    article: a per-item import must never fail over a cover.
    """
    return _is_injection_free(url) and url_safety.is_safe_mirror_source(url)


# --------------------------------------------------------------------------- #
# Pure builders                                                                #
# --------------------------------------------------------------------------- #

def build_note(
    content: str,
    pubkey_hex: str,
    *,
    extra_tags: Optional[List[List[str]]] = None,
    media: Sequence[PublishedMedia] = (),
) -> dict:
    """Construct an unsigned kind 1 event ready for a remote signer.

    Everyone ``content`` mentions (its ``nostr:npub``/``nprofile`` links,
    written there as the sheet's mentions are) gets a ``p`` tag, once,
    with the relay hint the link carries.

    ``media`` describes images this app resolved into URLs already in
    ``content``; the imeta tags are built here rather than threaded
    through ``extra_tags`` because only this function holds the final
    content, and the "URL is in the content" check has to run against it.

    Every note or article the content quotes (a ``nostr:`` link to it)
    gets a ``q`` tag, and its author a ``p`` tag (refs.quote_tags).

    Always attaches ``["client", CLIENT_NAME]`` so readers that honour the
    NIP-89 client tag display "Published from MyEditor" under the note.
    """
    p_tags = _mention_tags(content, markdown=False)
    tags: List[List[str]] = [["client", CLIENT_NAME]]
    tags.extend(p_tags)
    # Quoted notes and articles (NIP-18 q tags) and their authors.
    tags.extend(refs.quote_tags(refs.find_references(content, markdown=False),
                                [tag[1] for tag in p_tags]))
    tags.extend(build_imeta_tags(media, content))
    if extra_tags:
        tags.extend(extra_tags)
    return build_event(
        kind=1,
        content=content,
        tags=tags,
        pubkey_hex=pubkey_hex,
    )


def build_article(
    content: str,
    pubkey_hex: str,
    slug: str,
    *,
    title: str = "",
    summary: str = "",
    image: str = "",
    published_at: Optional[int] = None,
    hashtags: Iterable[str] = (),
    extra_tags: Optional[List[List[str]]] = None,
    media: Sequence[PublishedMedia] = (),
) -> dict:
    """Construct an unsigned NIP-23 long-form article (kind 30023).

    The ``slug`` becomes the ``d``-tag, the identifier that makes this
    event addressable. Re-publishing with the same slug replaces the
    previous version on relays that honour parameterized replacement.
    The ``d`` tag is the first tag: rnostr takes the first tag's value as
    the address.

    Everyone ``content`` mentions gets a ``p`` tag, as in ``build_note``
    (outside code: a link in code mentions nobody).

    Per NIP-23, ``content`` is Markdown and clients MUST NOT hard line-break
    paragraphs or accept HTML. This does not transform the content; that is the
    caller's responsibility.

    ``image`` is the cover. It is gated through
    :func:`_is_publishable_cover` because an imported article's cover
    comes from untrusted feed HTML; a refused address drops the tag and
    still publishes the article.

    NIP-94's note that it is "not expected to be implemented by ...
    longform clients that deal with kind:30023 articles" is about kind
    1063 file-metadata events, not about ``imeta`` tags, which NIP-92
    defines for any event. Emitting imeta on 30023 is correct.
    """
    if not slug.strip():
        raise ValueError("article slug (d-tag) must not be empty")
    p_tags = _mention_tags(content, markdown=True)
    # The address first: some relays (rnostr) take the first tag as the
    # address, so a "client" tag before it made each article replace the
    # author's last one there.
    tags: List[List[str]] = [
        ["d", slug.strip()],
        ["client", CLIENT_NAME],
    ]
    tags.extend(p_tags)
    # Quoted notes and articles (NIP-18 q tags) and their authors.
    tags.extend(refs.quote_tags(refs.find_references(content),
                                [tag[1] for tag in p_tags]))
    if title.strip():
        tags.append(["title", title.strip()])
    if summary.strip():
        tags.append(["summary", summary.strip()])
    cover = image.strip()
    if cover and _is_publishable_cover(cover):
        tags.append(["image", cover])
    if published_at is not None:
        tags.append(["published_at", str(int(published_at))])
    for raw in hashtags:
        tag = raw.strip().lstrip("#").lower()
        if tag:
            tags.append(["t", tag])
    tags.extend(build_imeta_tags(media, content))
    if extra_tags:
        tags.extend(extra_tags)
    tags = _one_tag_per_person(tags)
    return build_event(
        kind=30023,
        content=content,
        tags=tags,
        pubkey_hex=pubkey_hex,
    )


def without_secret_keys(unsigned: dict) -> Optional[dict]:
    """``unsigned`` with every plain Nostr secret key in its content and
    tags replaced by the link to the key's owner, built again so its id
    fits; None when one cannot be (an encrypted key names no owner). The
    ``d`` tag is never rewritten: it is the address the event is kept
    under, and a key there is for the caller to refuse."""
    content = refs.without_secret_keys(unsigned.get("content") or "")
    tags = [list(tag) if tag[:1] == ["d"] else
            [refs.without_secret_keys(value) for value in tag] for tag in unsigned["tags"]]
    if content is None or any(value is None for tag in tags for value in tag):
        return None
    return build_event(kind=unsigned["kind"], content=content, tags=tags,
                       pubkey_hex=unsigned["pubkey"], created_at=unsigned["created_at"])


# --------------------------------------------------------------------------- #
# When an article was first published                                         #
# --------------------------------------------------------------------------- #

def published_at_of(event: Optional[dict]) -> Optional[int]:
    """When an article was first published, read from a version of it: its
    ``published_at`` (NIP-23), or, for a version without one, the time
    that version was made, which is no later than the first publication
    of what it replaced. None for no event or no usable time."""
    if not isinstance(event, dict):
        return None
    for tag in event.get("tags", []) or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "published_at":
            try:
                value = int(str(tag[1]).strip())
            except ValueError:
                break
            if value > 0:
                return value
            break
    created = event.get("created_at")
    if isinstance(created, int) and not isinstance(created, bool) and created > 0:
        return created
    return None


# What the relays say about an article's first publication.
FOUND = "found"          # a version is there: it says when
NEVER = "never"          # relays that would hold it answered, and none has it
UNKNOWN = "unknown"      # too few relays answered to tell


@dataclass(frozen=True)
class FirstPublication:
    """When an article was first published, and how sure that is.
    ``under``: the other link name it was found under, when it was asked
    about with others (a file's own name); empty when it was found under
    the one asked about, or not found."""

    state: str
    published_at: Optional[int] = None
    under: str = ""


def first_publication_of(lookup, outbox: Sequence[str],
                         indexers: Sequence[str] = outbox_defaults.INDEXER_RELAYS
                         ) -> FirstPublication:
    """What the relays said about an article's first publication.

    FOUND with its date when any relay returned a version. NEVER (a new
    article) when none did and at least one of the author's own write
    relays answered, and, where the outbox layer names indexers, an
    indexer too: relays fail all the time, and one that is down must not
    turn every new article into a question. UNKNOWN only when the
    evidence is too thin: no write relay of the author answered at all
    (or a relay sent more candidates than could be checked)."""
    if lookup.event is not None:
        when = published_at_of(lookup.event)
        return FirstPublication(FOUND, when) if when else FirstPublication(UNKNOWN)
    answered = set(dedupe_relays(lookup.answered))
    write_answered = bool(answered & set(dedupe_relays(outbox)))
    indexed = not indexers or bool(answered & set(dedupe_relays(indexers)))
    if write_answered and indexed and not lookup.unchecked:
        return FirstPublication(NEVER)
    return FirstPublication(UNKNOWN)


def first_publication_among(found: Dict[str, Lookup], names: Sequence[str],
                            outbox: Sequence[str]) -> FirstPublication:
    """What the relays said about an article that may be out under any of
    ``names`` (in the order they count: a file's own names first, the
    link name asked about last). The first found is the article; with
    none found, NEVER only when the answers would say so for each. A
    file's own names were asked of the write relays alone, so the write
    relays' answers tell for them."""
    slug = names[-1]
    for name in names:
        lookup = found.get(name)
        if lookup is not None and lookup.event is not None:
            answer = first_publication_of(lookup, outbox)
            return FirstPublication(answer.state, answer.published_at,
                                    under="" if name == slug else name)
    answers = [first_publication_of(found[name], outbox, indexers=() if name != slug
                                    else outbox_defaults.INDEXER_RELAYS)
               for name in names if name in found]
    if answers and all(answer.state == NEVER for answer in answers):
        return FirstPublication(NEVER)
    return FirstPublication(UNKNOWN)


def find_first_publication(relay_pool: RelayPool, relay_directory: RelayDirectory,
                           author: str, slug: str,
                           on_done: Callable[[FirstPublication], None], *,
                           parent: QObject, timeout_ms: int = 4_000,
                           also: Sequence[str] = ()) -> None:
    """Whether and when the author's article ``slug`` was first published
    (FirstPublication). The question lives as long as ``parent`` (the
    dialog asking it).

    NIP-23 makes ``published_at`` the time of the first publication, so a
    new version of an article carries the old value: readers date it by
    that, and an edit must not move it. Asked of the relays the author
    publishes to, where the current version is, and also of the relays
    they read from and the indexers, so a relay list that moved since
    does not hide the first version.

    ``also``: other link names the article may be out under (a file's own
    name, which 3.3 published files under), counted before ``slug``
    (first_publication_among). They are asked of the write relays alone,
    where 3.3 published: those get one request about every name, the
    others one about ``slug``, so no name goes where it would not be
    published."""
    names = list(dict.fromkeys([*also, slug]))

    def got_relays(relays: List[str]) -> None:
        outbox = dedupe_relays(relays)
        asked = dedupe_relays([*outbox, *relay_directory.cached(author).read,
                               *outbox_defaults.INDEXER_RELAYS])
        if len(names) == 1:
            fetch_addressable(relay_pool, asked, kind=30023, author=author, d_tags=names,
                              on_done=lambda found: on_done(
                                  first_publication_among(found, names, outbox)),
                              timeout_ms=timeout_ms, parent=parent)
            return
        parts: Dict[str, Dict[str, Lookup]] = {}

        def answered(part: str, found: Dict[str, Lookup]) -> None:
            parts[part] = found
            if len(parts) == 2:
                on_done(first_publication_among(
                    _joined(parts["own"], parts["others"]), names, outbox))
        others = [url for url in asked if url not in outbox]
        fetch_addressable(relay_pool, outbox, kind=30023, author=author, d_tags=names,
                          on_done=lambda found: answered("own", found),
                          timeout_ms=timeout_ms, parent=parent)
        fetch_addressable(relay_pool, others, kind=30023, author=author, d_tags=[slug],
                          on_done=lambda found: answered("others", found),
                          timeout_ms=timeout_ms, parent=parent)
    relay_directory.outbox_of(author, got_relays)


def _joined(own: Dict[str, Lookup], others: Dict[str, Lookup]) -> Dict[str, Lookup]:
    """Two lookups' answers as one: per name, the newer version either
    found, and every relay that answered or refused in either."""
    joined = dict(own)
    for name, other in others.items():
        mine = own.get(name)
        if mine is None:
            joined[name] = other
            continue
        newer = (other.event if other.event is not None
                 and (mine.event is None or is_newer(other.event, mine.event)) else mine.event)
        joined[name] = Lookup(state=mine.state if newer is mine.event else other.state,
                              event=newer, answered=mine.answered + other.answered,
                              refused=mine.refused + other.refused,
                              unchecked=mine.unchecked or other.unchecked)
    return joined


# --------------------------------------------------------------------------- #
# Slug helper                                                                  #
# --------------------------------------------------------------------------- #

# A Nostr entity written in a title (a mention, a quote, a key): left out
# of the link name when it really is one (its checksum holds); a word that
# only starts like one ("Note10", "note1 on") stays.
_SLUG_ENTITY = re.compile(r"(?:nostr:)?((?:npub|nprofile|nsec|ncryptsec|note|nevent|naddr)1"
                          r"[0-9a-z]+)", re.IGNORECASE)
_SLUG_ENTITY_KINDS = ("npub", "nprofile", "nsec", "ncryptsec", "note", "nevent", "naddr")
# One shortened as a name it has none ("npub1abcd\u2026wxyz").
_SLUG_SHORT_KEY = re.compile(r"(?:npub|nprofile|note|nevent|naddr)1[0-9a-z]*\u2026[0-9a-z]+",
                             re.IGNORECASE)
# A hex key or hash: never a word.
_SLUG_HEX = re.compile(r"[0-9a-f]{32,}")
# Letters Unicode does not take apart into a letter and an accent.
_SLUG_LETTERS = str.maketrans({"\u0142": "l", "\u00f8": "o", "\u0111": "d", "\u00e6": "ae",
                               "\u0153": "oe", "\u00df": "ss", "\u1e9e": "ss",
                               "\u00fe": "th", "\u00f0": "d", "\u0131": "i",
                               "\u0127": "h", "\u0167": "t", "\u0140": "l"})
# Variation selectors (an emoji's text or picture presentation) and the
# keycap: never part of a word.
_SLUG_DROPPED = re.compile("[\ufe00-\ufe0f\U000e0100-\U000e01ef\u20e3]")
SLUG_MAX = 48


def _is_entity(word: str) -> bool:
    try:
        hrp, _data = bech32_decode(word)
    except ValueError:
        return False
    return hrp in _SLUG_ENTITY_KINDS


def link_name(typed: str, kept: str = "") -> str:
    """The link name a field holds, as it is published.

    One the article has already (``kept``: its draft's, its published
    version's, its file's), while the field still holds it, exactly as it
    is: relays compare a d tag letter for letter, and the same word
    written another way is another article (a Mac writes file names with
    "u" and an accent for "ü", and 3.3 published files under their names
    as they were). One typed or pasted by hand in the one form slugify
    writes too (NFC: "é" one character, as a title typed anywhere gives),
    so the same name is the same d tag wherever it was typed."""
    if kept and typed == kept:
        return kept
    return unicodedata.normalize("NFC", (typed or "").strip())


def file_link_names(file_name: str) -> List[str]:
    """The link names a file's article may be out under besides the one
    made of its name: the name as the file system gives it (3.3 published
    a file's article so; a Mac gives it with "u" and an accent for "ü"),
    then composed, each once."""
    raw = (file_name or "").strip()
    return list(dict.fromkeys(name for name in (raw, unicodedata.normalize("NFC", raw))
                              if name))


def slugify(text: str) -> str:
    """Turn a title (or a file's name) into the article's link name, its
    d-tag identifier.

    Lower case, words joined by hyphens. Latin letters are written without
    their accents ("Grüße" is "grusse", "Łódź" is "lodz"); a word in
    another script is kept as written (NIP-23 identifiers are any text,
    as Hugo and WordPress keep such slugs), so articles titled in
    Cyrillic or Japanese never share one name. A Nostr key, mention or
    quote in the title is left out (only one whose checksum holds; a word
    like "Note10" stays), and so is a hex key. A mark stays only with the
    letter or digit it follows; an emoji's variation selector, a keycap
    and a joiner are no words. At most ``SLUG_MAX``
    characters, cut after a word where one ends. A title with no words
    left (emoji only) gets a short name of its own from the title, never
    one shared by every such title; no title gives none.
    """
    if not (text or "").strip():
        return ""
    cleaned = _SLUG_SHORT_KEY.sub(" ", text)
    cleaned = _SLUG_ENTITY.sub(lambda found: " " if _is_entity(found.group(1))
                               else found.group(0), cleaned)
    cleaned = cleaned.lower().translate(_SLUG_LETTERS)
    kept: List[str] = []
    for char in unicodedata.normalize("NFKD", cleaned):
        if unicodedata.combining(char) and kept and kept[-1].isascii():
            continue                       # a Latin letter's accent
        kept.append(char)
    cleaned = unicodedata.normalize("NFC", "".join(kept))
    words, word = [], []
    for char in cleaned + " ":
        kind = unicodedata.category(char)[0]
        if _SLUG_DROPPED.match(char):
            continue                       # an emoji's presentation, a keycap
        if kind in "LN" or (kind == "M" and word):
            # A mark belongs to the letter or digit it follows, and to
            # nothing else.
            word.append(char)
        elif kind != "M" and word:
            words.append("".join(word))
            word = []
    words = [w for w in words if not _SLUG_HEX.fullmatch(w)]
    slug = "-".join(words)
    if len(slug) > SLUG_MAX:
        cut = slug[:SLUG_MAX + 1]
        slug = cut[:cut.rfind("-")] if "-" in cut else slug[:SLUG_MAX]
    if not slug:
        slug = "article-" + hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:8]
    return slug


# --------------------------------------------------------------------------- #
# PublishJob: outbox, sign, publish                                           #
# --------------------------------------------------------------------------- #

class PublishJob(PublishPipeline):
    """One end-to-end publish of a note, an article or any other public event.

    The shared pipeline (publish_pipeline.PublishPipeline): the routing and
    the signature at the same time, the signed event checked to be exactly
    the one asked for, then out to the relays the relay directory chose
    (RelayDirectory.publish_plan), never to relays of this job's choosing.

    Signals (fired in this order on the happy path):
      status_changed(str)     human-readable progress text
      signed(str)             event id (hex) of the signed event
      relay_result(str, bool, str)
      first_accept(str)
      completed(list)         final list of PublishResult tuples
                              [(url, ok, message), …], fired even when
                              zero relays accepted
      failed(str)             short reason; terminal. No further signals
                              after this

    Cancellation: ``cancel()`` stops the job where it is. Nothing is
    published after it and no further signal fires; a signer request
    already sent cannot be recalled, so its answer is ignored. Once the
    event has gone to the relays, cancelling only silences the job.
    """

    signed = Signal(str)

    def _signed(self, event: dict) -> None:
        self.signed.emit(event["id"])

    def _done_words(self, accepted: int, total: int) -> str:
        # Never which relay took it, or how many (lessons 26 and 36).
        return _("Published.") if accepted else ""

    def _answered(self, results: List[PublishResult]) -> None:
        # NIP-65: the mentioned people's relays that now hold this event
        # also get the author's relay list, so whoever reads it there can
        # find the rest of what the author writes.
        took_it = {normalize_relay_url(url) for url, ok, _message in results if ok}
        reached = [url for url in self.plan.inbox if url in took_it]
        if reached:
            self._relay_directory.share_relay_list(self._profile.user_pubkey, reached)


# --------------------------------------------------------------------------- #
# How large a draft may be                                                    #
# --------------------------------------------------------------------------- #

def draft_size_problem(inner: dict, profile, *,
                       key_here: Callable[[], bool] = lambda: False) -> Optional[str]:
    """Why ``inner`` is too large to save as a draft of ``profile``, in
    words to show, or None when it fits: ``signer_limits.size_problem``
    for its encrypted part (encrypted through a signer app and read back
    through it). ``key_here()`` says whether the account's key is also
    kept on this computer, which the words then offer. Raises what
    ``serialize_inner_event`` raises for an event it cannot serialize.
    """
    return size_problem(profile, draft=serialize_inner_event(inner), key_here=key_here)


# --------------------------------------------------------------------------- #
# DraftPublishJob: stash an unsigned inner event as a NIP-37 draft            #
# --------------------------------------------------------------------------- #

class DraftPublishJob(QObject):
    """End-to-end stash of one inner event as a NIP-37 draft wrap.

    Pipeline:
      1. Ask the relay directory where the account's private records live.
      2. Acquire bunker session.
      3. Bunker NIP-44 encrypts ``serialize_inner_event(inner)``.
      4. Wrap the ciphertext in a kind-31234 event.
      5. Bunker signs the wrap.
      6. Publish to the account's private relays
         (RelayDirectory.private_relays), all of which DraftSync reads
         drafts from, so every device of the author finds this one.

    Signals (in firing order on the happy path):
      status_changed(str)        progress text
      signed(dict)               the signed wrap, before it is sent: an
                                 import keeps it, so a retry sends this
                                 very event again (:meth:`send_signed`)
                                 instead of signing a new one
      stashed(str, str, int)     (identifier, event_id, created_at) on
                                 successful sign. Fires before relay
                                 results are in so the panel can update
                                 optimistically.
      completed(list)            list of PublishResult tuples
      failed(str)                terminal. No further signals after this

    A wrap sent again keeps its time, so if the person edited the draft
    meanwhile, the relays keep the edit: an older replaceable event
    loses (NIP-01). A retry can never overwrite an edit.

    ``expiration_seconds`` dates the draft's end (NIP-40); only imports
    pass it (drafts.DEFAULT_EXPIRATION_SECONDS). Without it the draft
    never expires, which is what a save from the editor makes, also of
    an imported draft: once the person saved it, it is theirs.

    Cancellation: ``cancel()`` flips a flag that suppresses all future
    signal emissions. The in-flight NIP-46 RPC can't actually be
    recalled, but the dialog (now destroyed) will no longer be
    notified, preventing "wrapped C/C++ object has been deleted"
    warnings on PySide6.
    """

    status_changed = Signal(str)
    signed = Signal(dict)
    stashed = Signal(str, str, int)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_directory: RelayDirectory,
        session_pool: BunkerSessionPool,
        profile: Profile,
        inner_event: Optional[dict],
        identifier: str,
        expiration_seconds: Optional[int] = None,
        extra_wrap_tags: Optional[List[List[str]]] = None,
        entitled_relays: Sequence[str] = (),
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        # ``inner_event`` may be None for a job that only sends a wrap
        # signed earlier (send_signed).
        if inner_event is not None and (
                inner_event.get("pubkey", "").lower() != profile.user_pubkey.lower()):
            raise ValueError(
                "inner event pubkey does not match the stashing profile"
            )
        if not identifier:
            raise ValueError("draft identifier (d-tag) must not be empty")
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        # Relays this account has standing on beyond its own list.
        self._entitled_relays = list(entitled_relays)
        self._profile = profile
        self._inner_event = inner_event
        self._identifier = identifier
        self._expiration_seconds = expiration_seconds
        self._extra_wrap_tags = extra_wrap_tags
        self._wrap_unsigned: Optional[dict] = None
        self._cancelled: bool = False

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        """Kick off the stash. Safe to call once per instance."""
        if self._inner_event is None:
            raise ValueError("no inner event to stash")
        self._emit_status(_("Looking up your relay list…"))
        ask_draft_relays(self._relay_directory, self._profile,
                         self._on_relays_ready, entitled=self._entitled_relays)

    def send_signed(self, signed_event: dict) -> None:
        """Send a wrap signed earlier (by :attr:`signed`) as it is: no
        encryption and no signature, so no signer prompt. Raises
        ValueError for a wrap that is not this account's draft
        ``identifier``."""
        tags = signed_event.get("tags") or []
        d_tag = next((t[1] for t in tags if isinstance(t, list) and len(t) >= 2
                      and t[0] == "d"), None)
        if (signed_event.get("kind") != DRAFT_WRAP_KIND
                or str(signed_event.get("pubkey", "")).lower()
                != self._profile.user_pubkey.lower()
                or d_tag != self._identifier
                or not verify_event(signed_event)):
            raise ValueError("not a signed draft of this account and identifier")
        self._emit_status(_("Looking up your relay list…"))
        ask_draft_relays(
            self._relay_directory, self._profile,
            lambda relays: self._on_signed(signed_event, relays, checkpoint=False),
            entitled=self._entitled_relays)

    def cancel(self) -> None:
        """Suppress further signal emissions; the in-flight RPC runs out."""
        self._cancelled = True

    # -- signal-emission helpers (guarded by ``_cancelled``) --------------

    def _emit_status(self, text: str) -> None:
        if not self._cancelled:
            self.status_changed.emit(text)

    def _emit_failed(self, reason: str) -> None:
        if not self._cancelled:
            self.failed.emit(_safe_reason(reason))

    def _refuse(self, words: str) -> None:
        """Fail with this app's own words, whole: clipped as a signer's
        reason is, they would end mid-sentence."""
        if not self._cancelled:
            self.failed.emit(words)

    # -- pipeline ----------------------------------------------------------

    def _on_relays_ready(self, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        self._emit_status(_("Connecting to your signer app…"))
        self._session_pool.get(
            self._profile,
            on_ready=lambda client: self._on_bunker_ready(client, publish_relays),
            on_error=self._emit_failed,
        )

    def _on_bunker_ready(self, client: BunkerClient, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        try:
            plaintext = serialize_inner_event(self._inner_event)
            # Pre-flight against what this account's signer can encrypt
            # (65,535 bytes here, 40,960 through a signer app, less over
            # relays that cap what they carry). The signer
            # would refuse larger payloads anyway; said here, the person
            # reads the real limit, and no approval prompt is spent on an
            # inevitable failure.
            too_large = draft_size_problem(
                self._inner_event, self._profile,
                key_here=lambda: self._session_pool.key_here(self._profile.user_pubkey))
        except (KeyError, TypeError, ValueError) as exc:
            self._emit_failed(_("Could not serialize draft: {reason}").format(reason=exc))
            return
        if too_large:
            self._refuse(too_large)             # one sentence that says so (lessons 36)
            return
        self._emit_status(
            _("Encrypting the draft. Approve the request in your signer app if it asks…")
        )
        client.nip44_encrypt_self(
            plaintext,
            on_success=lambda ct: self._on_encrypted(ct, client, publish_relays),
            on_failure=lambda reason: self._emit_failed(
                _("Could not encrypt draft: {reason}").format(reason=_safe_reason(reason))
            ),
        )

    def _on_encrypted(
        self,
        ciphertext: str,
        client: BunkerClient,
        publish_relays: List[str],
    ) -> None:
        if self._cancelled:
            return
        try:
            self._wrap_unsigned = build_draft_wrap(
                identifier=self._identifier,
                inner_kind=int(self._inner_event["kind"]),
                encrypted_content=ciphertext,
                pubkey_hex=self._profile.user_pubkey,
                client_name=CLIENT_NAME,
                expiration_seconds=self._expiration_seconds,
                extra_tags=self._extra_wrap_tags,
            )
        except ValueError as exc:
            self._emit_failed(_("Couldn’t prepare the draft: {reason}").format(reason=exc))
            return

        self._emit_status(
            _("Waiting for your signer app. Approve the request there…")
        )
        client.sign_event(
            self._wrap_unsigned,
            on_success=lambda signed: self._on_signed(signed, publish_relays),
            on_failure=self._emit_failed,
        )

    def _on_signed(self, signed_event: dict, publish_relays: List[str], *,
                   checkpoint: bool = True) -> None:
        if self._cancelled:
            return
        if checkpoint:
            self.signed.emit(dict(signed_event))
        self.stashed.emit(
            self._identifier,
            signed_event["id"],
            int(signed_event["created_at"]),
        )
        self._emit_status(ngettext(
            "Publishing draft to {n} relay…", "Publishing draft to {n} relays…",
            len(publish_relays),
        ).format(n=len(publish_relays)))
        job = self._relay_pool.publish(publish_relays, signed_event)
        job.all_done.connect(self._on_publish_done)

    def _on_publish_done(self, results: List[PublishResult]) -> None:
        if self._cancelled:
            return
        accepted = sum(1 for _url, ok, _message in results if ok)
        self._emit_status(ngettext(
            "Draft saved to {accepted}/{total} relay.",
            "Draft saved to {accepted}/{total} relays.", len(results),
        ).format(accepted=accepted, total=len(results)))
        self.completed.emit(results)


# --------------------------------------------------------------------------- #
# DraftDeleteJob: tombstone an existing draft                                 #
# --------------------------------------------------------------------------- #

class DraftDeleteJob(QObject):
    """Publish a blank-content replacement wrap to soft-delete a draft.

    Per NIP-37 the deletion mechanism is *not* NIP-09; the addressable
    event is replaced with one whose ``content`` is empty. Same shape
    as ``DraftPublishJob`` minus the encryption step (no plaintext). It
    goes to every relay drafts are read from, so the replacement lands
    wherever the draft may be, including where it went while the
    account's own list was unknown.

    ``announce`` also sends a NIP-09 deletion request for the draft, signed
    right after the tombstone and sent to the same relays. Apps that
    delete that way (EINUNDZWANZIG STANDUP) read only those, and an
    imported post deleted without one could be imported there again.
    ``replaced_wrap_id`` names the wrap being deleted in that request.

    Signals:
      status_changed(str)
      tombstoned(str, str)       (identifier, event_id) right after the
                                 signer returns the signed tombstone,
                                 lets the panel remove the row before
                                 relay results land.
      completed(list)            list of PublishResult tuples
      failed(str)
    """

    status_changed = Signal(str)
    tombstoned = Signal(str, str)
    completed = Signal(list)
    failed = Signal(str)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_directory: RelayDirectory,
        session_pool: BunkerSessionPool,
        profile: Profile,
        identifier: str,
        inner_kind: int,
        entitled_relays: Sequence[str] = (),
        announce: bool = False,
        replaced_wrap_id: str = "",
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        if not identifier:
            raise ValueError("draft identifier (d-tag) must not be empty")
        if inner_kind not in SUPPORTED_INNER_KINDS:
            raise ValueError(
                f"unsupported inner kind {inner_kind!r}; "
                f"expected one of {SUPPORTED_INNER_KINDS}"
            )
        self._announce = announce
        self._replaced_wrap_id = replaced_wrap_id
        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        # Relays this account has standing on beyond its own list.
        self._entitled_relays = list(entitled_relays)
        self._profile = profile
        self._identifier = identifier
        self._inner_kind = inner_kind
        self._cancelled: bool = False

    # -- public API --------------------------------------------------------

    def start(self) -> None:
        self._emit_status(_("Looking up your relay list…"))
        # Wherever the draft may be, including the relays it went to while
        # the account's own list was unknown: the tombstone replaces it there.
        ask_draft_relays(self._relay_directory, self._profile,
                         self._on_relays_ready, entitled=self._entitled_relays,
                         reading=True)

    def cancel(self) -> None:
        self._cancelled = True

    # -- guarded emit helpers ---------------------------------------------

    def _emit_status(self, text: str) -> None:
        if not self._cancelled:
            self.status_changed.emit(text)

    def _emit_failed(self, reason: str) -> None:
        if not self._cancelled:
            self.failed.emit(_safe_reason(reason))

    # -- pipeline ----------------------------------------------------------

    def _on_relays_ready(self, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        self._session_pool.get(
            self._profile,
            on_ready=lambda client: self._on_bunker_ready(client, publish_relays),
            on_error=self._emit_failed,
        )

    def _on_bunker_ready(self, client: BunkerClient, publish_relays: List[str]) -> None:
        if self._cancelled:
            return
        if refs.secret_keys_in(self._identifier):
            self._delete_by_wrap_id(client, publish_relays)
            return
        unsigned = build_tombstone_wrap(
            identifier=self._identifier,
            inner_kind=self._inner_kind,
            pubkey_hex=self._profile.user_pubkey,
            client_name=CLIENT_NAME,
        )
        self._emit_status(
            _("Waiting for your signer app. Approve the deletion there…")
        )
        client.sign_event(
            unsigned,
            on_success=lambda signed: self._on_signed(signed, publish_relays, client),
            on_failure=self._emit_failed,
        )

    def _on_signed(self, signed_event: dict, publish_relays: List[str],
                   client: Optional[BunkerClient] = None) -> None:
        if self._cancelled:
            return
        if self._announce and client is not None:
            # Signed before anything is sent, so the signer is asked one
            # thing at a time. The draft is deleted by the tombstone
            # alone; a refused request only means STANDUP does not hear.
            # One second before the tombstone (engine review L3): a relay
            # that follows NIP-09 deletes every version up to the request,
            # and one in the same second took the tombstone with it. The
            # tombstone already came after the draft it replaces, so the
            # request still names everything before it.
            request = build_deletion_request(
                pubkey_hex=self._profile.user_pubkey, identifier=self._identifier,
                wrap_id=self._replaced_wrap_id,
                created_at=int(signed_event["created_at"]) - 1)
            client.sign_event(
                request,
                on_success=lambda signed_request: self._send(
                    signed_event, publish_relays, signed_request),
                on_failure=lambda _reason: self._send(signed_event, publish_relays))
            return
        self._send(signed_event, publish_relays)

    def _delete_by_wrap_id(self, client: BunkerClient, publish_relays: List[str]) -> None:
        """A draft whose slug holds a secret key: a tombstone would carry
        the key again (as its d tag), so the draft's wrap is deleted by its
        id alone, with a NIP-09 request that names no address."""
        if not self._replaced_wrap_id:
            self._emit_failed(_("This draft can’t be deleted: its link name holds a secret key, "
                                "and the version on your relays isn’t known here. Refresh "
                                "your drafts and try again."))
            return
        request = build_deletion_request(pubkey_hex=self._profile.user_pubkey,
                                         identifier=self._identifier,
                                         wrap_id=self._replaced_wrap_id)
        self._emit_status(_("Waiting for your signer app. Approve the deletion there…"))
        client.sign_event(request,
                          on_success=lambda signed: self._send(signed, publish_relays),
                          on_failure=self._emit_failed)

    def _send(self, signed_event: dict, publish_relays: List[str],
              deletion_request: Optional[dict] = None) -> None:
        if self._cancelled:
            return
        self.tombstoned.emit(self._identifier, signed_event["id"])
        self._emit_status(ngettext(
            "Removing draft from {n} relay…", "Removing draft from {n} relays…",
            len(publish_relays),
        ).format(n=len(publish_relays)))
        job = self._relay_pool.publish(publish_relays, signed_event)
        job.all_done.connect(self._on_publish_done)
        # The tombstone first, then the request that names the draft.
        if deletion_request is not None:
            self._relay_pool.publish(publish_relays, deletion_request)

    def _on_publish_done(self, results: List[PublishResult]) -> None:
        if self._cancelled:
            return
        accepted = sum(1 for _url, ok, _message in results if ok)
        self._emit_status(ngettext(
            "Draft removed on {accepted}/{total} relay.",
            "Draft removed on {accepted}/{total} relays.", len(results),
        ).format(accepted=accepted, total=len(results)))
        self.completed.emit(results)


class DraftBulkDeleteJob(QObject):
    """Delete several drafts, one signer round-trip at a time.

    Sequential rather than parallel, and not as a performance choice.
    Every deletion needs its own signature, and a remote signer answers
    one request at a time whatever this end does; firing all of them at
    once would put N prompts on the user's phone in an order nobody
    chose, and take away the ability to stop after the third.

    One failure does not end the run. The user asked for these to go,
    and the ones that can go should. What they get at the end is a count
    of what did not, because a partial result reported as a whole one is
    the version of this that loses drafts quietly.

    Cancelling stops the run before the next signature is requested. It
    cannot recall a deletion already signed and published, so the count
    in ``finished`` is what actually happened rather than what was asked
    for.

    Signals:
      progress(int, int)   drafts settled so far, total asked for.
      tombstoned(str, str) (identifier, event_id) as each one is signed,
                           forwarded so the panel can drop the row before
                           the relay results land.
      finished(int, list)  how many were deleted, and the
                           (identifier, reason) pairs for those that
                           were not.
    """

    progress = Signal(int, int)
    tombstoned = Signal(str, str)
    finished = Signal(int, list)

    def __init__(
        self,
        *,
        relay_pool: RelayPool,
        relay_directory: RelayDirectory,
        session_pool: BunkerSessionPool,
        profile: Profile,
        targets: Sequence[Tuple[str, int]],
        entitled_relays: Sequence[str] = (),
        announce: Optional[dict] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        # Identifier -> id of its current wrap, for the drafts whose
        # deletion is also announced (DraftDeleteJob ``announce``).
        self._announce = dict(announce or {})
        # Validated here, before a single signature is asked for. A run
        # that would die on its ninth draft because of something knowable
        # at the start has already cost the user eight approvals.
        self._targets: List[Tuple[str, int]] = []
        for identifier, inner_kind in targets:
            if not identifier:
                raise ValueError("draft identifier (d-tag) must not be empty")
            if inner_kind not in SUPPORTED_INNER_KINDS:
                raise ValueError(
                    f"unsupported inner kind {inner_kind!r}; "
                    f"expected one of {SUPPORTED_INNER_KINDS}"
                )
            self._targets.append((identifier, int(inner_kind)))

        self._relay_pool = relay_pool
        self._relay_directory = relay_directory
        self._session_pool = session_pool
        self._entitled_relays = list(entitled_relays)
        self._profile = profile

        self._index: int = 0
        self._deleted: int = 0
        self._failures: List[Tuple[str, str]] = []
        self._current: Optional[DraftDeleteJob] = None
        self._cancelled: bool = False
        # Re-entrancy guard, the same shape the decrypt queues use: a
        # signer that answers synchronously would otherwise recurse once
        # per draft.
        self._pumping: bool = False
        self._inflight: bool = False
        self._done: bool = False

    # -- public API --------------------------------------------------------

    @property
    def total(self) -> int:
        return len(self._targets)

    def start(self) -> None:
        if not self._targets:
            self._finish()
            return
        self._pump()

    def cancel(self) -> None:
        """Stop before the next signature is asked for.

        The in-flight deletion is cancelled too, but it may already be
        signed and on its way to the relays, which is why this reports
        rather than promises.
        """
        if self._cancelled or self._done:
            return
        self._cancelled = True
        if self._current is not None:
            self._current.cancel()
            self._current = None
        self._finish()

    # -- the queue ---------------------------------------------------------

    def _pump(self) -> None:
        if self._pumping or self._inflight or self._done:
            return
        self._pumping = True
        try:
            while (
                not self._cancelled
                and not self._inflight
                and self._index < len(self._targets)
            ):
                identifier, inner_kind = self._targets[self._index]
                self._inflight = True
                self._start_one(identifier, inner_kind)
        finally:
            self._pumping = False
        if not self._inflight and not self._cancelled:
            self._finish()

    def _start_one(self, identifier: str, inner_kind: int) -> None:
        try:
            job = DraftDeleteJob(
                relay_pool=self._relay_pool,
                relay_directory=self._relay_directory,
                session_pool=self._session_pool,
                profile=self._profile,
                identifier=identifier,
                inner_kind=inner_kind,
                entitled_relays=self._entitled_relays,
                announce=identifier in self._announce,
                replaced_wrap_id=self._announce.get(identifier, ""),
                parent=self,
            )
        except ValueError as exc:
            # Cannot happen for targets that passed the constructor, but
            # a run that dies here would strand the whole queue.
            self._settle(identifier, reason=str(exc))
            return

        self._current = job
        job.tombstoned.connect(self.tombstoned)
        job.completed.connect(
            lambda results, ident=identifier: self._on_one_completed(ident, results)
        )
        job.failed.connect(
            lambda reason, ident=identifier: self._settle(ident, reason=reason)
        )
        job.start()

    def _on_one_completed(self, identifier: str, results: List[PublishResult]) -> None:
        # ``completed`` fires even when every relay refused, so the
        # results decide whether this counts as deleted. Reporting a
        # deletion no relay accepted would tell the user a draft is gone
        # from a place it is still on.
        if any(ok for _url, ok, _message in results):
            self._settle(identifier)
            return
        reasons = "; ".join(msg for _url, ok, msg in results if not ok and msg)
        if reasons:
            reason = _("no relay accepted the deletion ({reasons})").format(reasons=reasons)
        else:
            reason = _("no relay accepted the deletion")
        self._settle(identifier, reason=reason)

    def _settle(self, identifier: str, *, reason: str = "") -> None:
        if self._done:
            return
        if reason:
            self._failures.append((identifier, _safe_reason(reason)))
        else:
            self._deleted += 1
        self._current = None
        self._index += 1
        self._inflight = False
        self.progress.emit(self._index, len(self._targets))
        if self._cancelled:
            return
        self._pump()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit(self._deleted, list(self._failures))


def _one_tag_per_person(tags: List[List[str]]) -> List[List[str]]:
    """``tags`` with one ``p`` tag per person: a co-author also mentioned in
    the text keeps the tag that names them a co-author (with its marker),
    and the mention's relay hint when that one has none."""
    marked = {tag[1]: tag for tag in tags
              if tag[0] == "p" and len(tag) >= 4 and tag[3] == AUTHOR_MARKER}
    out: List[List[str]] = []
    seen = set()
    for tag in tags:
        if tag[0] != "p" or len(tag) < 2:
            out.append(tag)
            continue
        person = tag[1]
        if person in seen:
            continue
        seen.add(person)
        keep = marked.get(person, tag)
        if keep is not tag and len(tag) >= 3 and tag[2] and not keep[2]:
            keep = ["p", person, tag[2], AUTHOR_MARKER]
        out.append(list(keep))
    return out
