# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The published work of an accepted submission (plan 8.2, 8.5, 8.6;
editorial contract 5, 7, 12.4): the kind 30023 template a writer signs
under their own key, built from an opened, verified revision, as the
web app builds it (``publish-event.js``, ``publish-media.js``). Pure: it
signs, uploads and sends nothing.

- Every co-author of ``work.authors`` and contributor of ``credits`` is
  credited as ``["p", pk, relay, "author"]``; the writer is named first
  when others wrote it too. Parts and roles stay private.
- A writer who signs keeps the split their own app wrote into the work
  (its ``zap`` tags); the split of an accept is for a work the
  publication signs, which this app never publishes.
- Carried tags keep what the writer's app added, never ``expiration``,
  ``zap`` or an author tag (only the verified parts write those).
- Every ``standup-media:<sha>`` the text, the cover and the tags name
  (a photo group's ``imeta``) is replaced by its public address; in an
  ``imeta`` its ``x`` becomes the plain file's hash. A ref without an
  address is refused, so nothing private-looking is published.
- A correction replaces the work at its address and keeps
  ``published_at`` (``publisher.published_at_of``). The Redaktion writes
  it and the writer signs it, so it changes only what the writer sees
  before Korrektur übernehmen (RG-M4-a 1): the text (with what the text
  names), the title, the summary, the cover, the topics and the
  language. The published event's zaps (a split never changes through a
  correction), credits, co-authors and carried tags stay as they are.
  Each correction is compared with the article as it stands in public,
  never with an earlier correction (RG-M4-a 6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .. import events as nostr_events
from .. import publisher, zap_shares
from ..article_details import AUTHOR_MARKER
from ..bech32 import decode_naddr
from . import following as rules
from . import formats, views
from .formats import FormatError
from .former import Formers
from .confirmations import (
    Confirmation, correction_taken, published_revision, to_confirm, verified_confirmations,
)
from .pictures import imeta_url, one_per_address, picture_imeta
from .release import Release, credited_writer, held_credit, release

# A revision names at most this many people and carried tags (5).
_PEOPLE_MAX = 50
_EXTRA_TAGS_MAX = 100
_PART_MAX = 1024
NEVER_CARRIED = frozenset({"expiration", "zap"})
_MEDIA_REF = re.compile(r"standup-media:[0-9a-f]{64}")
_IMETA_URL = re.compile(r"^url (standup-media:[0-9a-f]{64})$")
# The ending a plain file goes up under: its type's, never its name's.
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
              "image/webp": "webp", "image/avif": "avif"}
LANGUAGE_NAMESPACE = "ISO-639-1"


class NotPublishable(ValueError):
    """The revision cannot be published as it is: ``code`` says why
    (``invalid``, ``media_missing``, ``media_type``)."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def _text(value, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > _PART_MAX * 1024:
        if required:
            raise NotPublishable("invalid", "the work misses a field")
        return ""
    return value


def _is_tag(tag) -> bool:
    return (isinstance(tag, list) and 0 < len(tag) <= 16 and tag[0] != ""
            and all(isinstance(part, str) and len(part) <= _PART_MAX for part in tag))


def work_of(revision: Mapping) -> dict:
    """The work of an opened revision, checked at the boundary."""
    work = revision.get("work") if isinstance(revision, Mapping) else None
    if not isinstance(work, Mapping):
        raise NotPublishable("invalid", "the revision holds no work")
    d, title = _text(work.get("d"), required=True), _text(work.get("title"), required=True)
    if not d or len(d) > _PART_MAX or not title.strip() or len(title) > _PART_MAX:
        raise NotPublishable("invalid", "the work has no address or no title")
    content = _text(work.get("content"), required=True)
    authors = work.get("authors") or []
    hashtags = work.get("tags") or []
    extra = work.get("extra_tags") or []
    if (not isinstance(authors, list) or len(authors) > _PEOPLE_MAX
            or not all(isinstance(a, Mapping) and formats.is_hex64(a.get("pubkey"))
                       for a in authors)):
        raise NotPublishable("invalid", "the co-authors are not a list of people")
    if not isinstance(hashtags, list) or not all(isinstance(t, str) and len(t) <= _PART_MAX
                                                 for t in hashtags):
        raise NotPublishable("invalid", "the topics are not a list of words")
    if not isinstance(extra, list) or len(extra) > _EXTRA_TAGS_MAX or not all(map(_is_tag, extra)):
        raise NotPublishable("invalid", "the work carries tags that are none")
    return {"d": d, "title": title.strip(), "summary": _text(work.get("summary")),
            "image": _text(work.get("image")), "tags": [t for t in hashtags if t],
            "lang": _text(work.get("lang")), "content": content,
            "authors": [(a["pubkey"], a.get("relay") if isinstance(a.get("relay"), str) else "")
                        for a in authors],
            "extra_tags": [list(tag) for tag in extra]}


def media_refs_in(revision: Mapping) -> List[str]:
    """The encrypted files the work's cover, text and tags name, each once,
    in the order they appear."""
    work = work_of(revision)
    found = [work["image"]] if _MEDIA_REF.fullmatch(work["image"]) else []
    found += _MEDIA_REF.findall(work["content"])
    for tag in work["extra_tags"]:
        for part in tag:
            found += _MEDIA_REF.findall(part)
    return list(dict.fromkeys(found))


def rights_of(revision: Mapping, person: str) -> Dict[str, str]:
    """``{"part", "license"}`` a contributor confirms: their part as the
    revision credits it (``credits[].part``) and the license it names
    (``rights.license``), each ``""`` when none (RG-M4-b 7)."""
    credits = revision.get("credits") if isinstance(revision.get("credits"), list) else []
    part = next((c.get("part") for c in credits if isinstance(c, Mapping)
                 and c.get("pubkey") == person and isinstance(c.get("part"), str)), "")
    rights = revision.get("rights") if isinstance(revision.get("rights"), Mapping) else {}
    license = rights.get("license") if isinstance(rights.get("license"), str) else ""
    return {"part": part.strip()[:_PART_MAX], "license": license.strip()[:_PART_MAX]}


def media_to_publish(revision: Mapping) -> List[Mapping]:
    """The media entries the work needs in public: one per ref it names,
    held by the revision under exactly that file's hash, of a type that is
    published."""
    entries = [e for e in revision.get("media") or () if isinstance(e, Mapping)]
    wanted = []
    for ref in media_refs_in(revision):
        entry = next((e for e in entries if e.get("ref") == ref), None)
        if entry is None or ref != formats.media_ref(str(entry.get("cipher_sha256"))):
            raise NotPublishable("media_missing", "the work names a file its revision lacks")
        if entry.get("mime") not in EXTENSIONS or not formats.is_hex64(entry.get("plain_sha256")):
            raise NotPublishable("media_type", "the work holds a file that is not published")
        wanted.append(entry)
    return wanted


def served_at(server: str, entry: Mapping) -> str:
    """Where a media server serves a plain file (BUD-01): its hash and
    its type's ending."""
    return f"{server.rstrip('/')}/{entry['plain_sha256']}.{EXTENSIONS[entry['mime']]}"


def addresses_before(entries: Sequence[Mapping], servers: Sequence[str]) -> Dict[str, str]:
    """``{ref: address}`` as long as the files' addresses will be on the
    longest of ``servers``: what a signer's length check measures before
    anything goes up, so nothing private is put up for a work the signer
    could not sign."""
    longest = max((s.rstrip("/") for s in servers if s), key=len, default="https://media.invalid")
    return {entry["ref"]: served_at(longest, entry) for entry in entries}


def _with_public_media(text: str, urls: Mapping[str, str]) -> str:
    def public(match) -> str:
        url = urls.get(match.group(0))
        if not url:
            raise NotPublishable("media_missing", "a picture of the work has no address yet")
        return url
    return _MEDIA_REF.sub(public, text)


def _carried(tag: List[str], urls: Mapping[str, str], media: Sequence[Mapping]) -> List[str]:
    parts = [_with_public_media(part, urls) if "standup-media:" in part else part for part in tag]
    if tag[0] != "imeta":
        return parts
    ref = next((m.group(1) for m in map(_IMETA_URL.match, tag) if m), None)
    entry = next((e for e in media if e.get("ref") == ref), None) if ref else None
    if entry is None:
        return parts
    return [f"x {entry['plain_sha256']}" if part == f"x {entry['cipher_sha256']}" else part
            for part in parts]


def _credited(work: dict, revision: Mapping, writer: str) -> List[List[str]]:
    credits = revision.get("credits") or []
    if not isinstance(credits, list) or len(credits) > _PEOPLE_MAX or not all(
            isinstance(c, Mapping) and formats.is_hex64(c.get("pubkey")) for c in credits):
        raise NotPublishable("invalid", "the contributors are not a list of people")
    people: Dict[str, str] = {}
    for pubkey, relay in work["authors"]:
        people.setdefault(pubkey, relay)
    for credit in credits:
        people.setdefault(credit["pubkey"], "")
    people.pop(writer, None)
    return [["p", pk, relay, AUTHOR_MARKER] for pk, relay in people.items()]


def credited(revision: Mapping, writer: str) -> List[str]:
    """Everyone the published work credits besides ``writer``, in the
    order the writer named them (for the sheet's Personen); [] for a work
    that cannot be published as it is."""
    try:
        return [tag[1] for tag in _credited(work_of(revision), revision, writer)]
    except NotPublishable:
        return []


def _language_tags(lang: str) -> List[List[str]]:
    code = lang.strip().lower()
    if not re.fullmatch(r"[a-z]{2}", code):
        return []
    return [["L", LANGUAGE_NAMESPACE], ["l", code, LANGUAGE_NAMESPACE]]


def published_template(revision: Mapping, *, writer: str, d: str,
                       media_urls: Mapping[str, str], published_at: int, created_at: int,
                       fallback_relay: str = "") -> dict:
    """The kind 30023 template of ``revision`` (opened from the verified
    record) that ``writer`` signs, at the address ``d`` (the work's own:
    the server checks the coordinate, 12.4), with ``media_urls`` naming
    every encrypted file's public address."""
    if not formats.is_hex64(writer) or not isinstance(published_at, int) \
            or not isinstance(created_at, int):
        raise NotPublishable("invalid", "not a key or not a time")
    work = work_of(revision)
    media = [e for e in revision.get("media") or () if isinstance(e, Mapping)]
    others = _credited(work, revision, writer)
    carried = [_carried(tag, media_urls, media) for tag in work["extra_tags"]
               if tag[0] not in NEVER_CARRIED
               and not (tag[0] == "p" and len(tag) >= 4 and tag[3] == AUTHOR_MARKER)]
    # One imeta per picture, from its entry (5), before the carried tags:
    # a carried imeta for the same address goes (one per address, 92.md).
    carried = one_per_address(picture_imeta(media, media_urls) + carried)
    own_zaps = [["zap", pk, relay or fallback_relay, _weight(weight)]
                for pk, relay, weight in zap_shares.read_zaps(work["extra_tags"])]
    people = ([["p", writer, "", AUTHOR_MARKER]] if others else []) + others
    image = _with_public_media(work["image"], media_urls) if work["image"] else ""
    event = publisher.build_article(
        _with_public_media(work["content"], media_urls), writer, d, title=work["title"],
        summary=work["summary"], image=image, published_at=published_at,
        hashtags=work["tags"],
        extra_tags=people + own_zaps + _language_tags(work["lang"]) + carried)
    event["created_at"] = created_at
    return event


def _weight(weight: float) -> str:
    return str(int(weight)) if float(weight).is_integer() else str(weight)


@dataclass(frozen=True)
class Accepted:
    """The verified accept a publish or a confirmation rests on (9.4,
    12.4): the revision it accepted (its signed ``expected_revision``),
    the id of the split it named, who decided, its ``seq``, and the
    section it named."""

    revision: int
    split_rumor_id: Optional[str]
    actor: str
    seq: int
    section: str = ""


def verified_accept(items: Sequence[views.TimelineItem], sid: str,
                    deciders: Optional[Iterable[str]],
                    checks: Optional[rules.SignedChecks] = None,
                    formers: Optional[Formers] = None) -> Optional[Accepted]:
    """The newest decision of ``items`` when it is an accept that verifies
    as one of ``deciders``, read from its signed body; else None (another
    decision came later, it does not verify, or the members are not
    known); ``formers`` verify people who left. Nothing that rests on the
    Redaktion's word takes any other."""
    decisions = [item for item in items
                 if item.event is not None and item.event.name in rules.DECISIONS]
    if not decisions or deciders is None:
        return None
    item = max(decisions, key=lambda each: each.seq)
    event = item.event
    if event.name != "accept" or not rules.action_verified(event, sid, tuple(deciders), checks,
                                                           formers):
        return None
    body = (checks or rules.SignedChecks()).body(
        event.signed, path=views.action_path(event.name, sid), sid=sid) or {}
    revision, split = body.get("expected_revision"), body.get("split_rumor_id")
    section = body.get("section")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    return Accepted(revision=revision, split_rumor_id=split if isinstance(split, str) else None,
                    actor=event.actor or "", seq=item.seq,
                    section=section if isinstance(section, str) else "")



@dataclass(frozen=True)
class Published:
    """A published work whose own event proves its address (contract 10,
    The address of a published work): ``address`` is ``30023:<pk>:<d>``."""

    address: str
    event: dict
    seq: int
    revision: Optional[int] = None      # the revision its report names


def work_address(signed: views.SignedRecord, sid: str, *, writer: str, coordinate_hash: str,
                 publication: str, checks: Optional[rules.SignedChecks] = None
                 ) -> Optional[Published]:
    """The address a ``published`` report proves, whoever reported it, as
    ``publishedAddress`` in the contract's crosscheck.reference.mjs has it
    (vector ``published_address``): the report verifies as an event for
    this submission; its ``event`` is a valid kind 30023 that its ``naddr``
    names; and it is the writer's at the work's ``d`` (``coordinate_hash``
    of the verified revision), or the publication's crediting the writer
    first (:func:`credited_writer`) at that ``d`` or at that ``d`` with
    ``-<sid8>``. None otherwise."""
    body = (checks or rules.SignedChecks()).body(
        signed, path=views.action_path("published", sid), sid=sid) or {}
    work, naddr = body.get("event"), body.get("naddr")
    if not nostr_events.is_verified_event(work, kind=formats.ARTICLE_KIND) \
            or not isinstance(naddr, str):
        return None
    d = (formats.tag_values(work, "d") or [None])[0]
    try:
        identifier, author, kind, _relays = decode_naddr(naddr)
        publication_key = formats.parse_address(publication)[0]
    except (ValueError, FormatError):
        return None
    if not isinstance(d, str) or (identifier, author, kind) != (d, work["pubkey"],
                                                                formats.ARTICLE_KIND):
        return None

    def own(value: str) -> bool:
        return formats.coordinate_hash(writer, value) == coordinate_hash

    ending = "-" + sid[:8]
    proven = own(d) if work["pubkey"] == writer else (
        work["pubkey"] == publication_key and credited_writer(work) == writer
        and (own(d) or (d.endswith(ending) and own(d[:-len(ending)]))))
    return Published(f"{formats.ARTICLE_KIND}:{work['pubkey']}:{d}", dict(work), 0) \
        if proven else None


def placed_section(signed: views.SignedRecord, sid: str, address: str, publication: str,
                   checks: Optional[rules.SignedChecks] = None) -> Optional[str]:
    """The section a ``placed`` report names for the work at ``address``
    (the reporter's word under their signature, ``""`` for none), when
    its ``site_event`` is a valid kind 30004 by the publication key with
    the Site's ``d`` and an ``a`` tag for that address; else None
    (contract 10, as ``placedSection`` in crosscheck.reference.mjs)."""
    body = (checks or rules.SignedChecks()).body(
        signed, path=views.action_path("placed", sid), sid=sid) or {}
    site = body.get("site_event")
    try:
        key, site_d = formats.parse_address(publication)
    except FormatError:
        return None
    if not nostr_events.is_verified_event(site, kind=formats.SITE_KIND) \
            or site["pubkey"] != key or (formats.tag_values(site, "d") or [None])[0] != site_d \
            or not any(tag[:2] == ["a", address] for tag in site["tags"]):
        return None
    section = body.get("section")
    return section if isinstance(section, str) else ""


def published_work(items: Sequence[views.TimelineItem], sid: str, *, writer: str,
                   coordinate_hash: str, publication: str,
                   checks: Optional[rules.SignedChecks] = None, accepted: Optional[int] = None,
                   corrections: Optional[Mapping[str, int]] = None) -> Optional[Published]:
    """The newest ``published`` report whose event proves the address
    (:func:`work_address`), or None. Never a server's word alone; its
    revision neither (:func:`published_revision`)."""
    checks = checks or rules.SignedChecks()
    for item in sorted(items, key=lambda each: each.seq, reverse=True):
        event = item.event
        if event is not None and event.name == "published" and event.signed is not None:
            found = work_address(event.signed, sid, writer=writer,
                                 coordinate_hash=coordinate_hash, publication=publication,
                                 checks=checks)
            if found is not None:
                return Published(found.address, found.event, item.seq, published_revision(
                    items, sid, item, writer=writer, accepted=accepted,
                    corrections=corrections or {}, checks=checks))
    return None


def section_of(items: Sequence[views.TimelineItem], sid: str, address: str, publication: str,
               checks: Optional[rules.SignedChecks] = None) -> Optional[str]:
    """The section of the newest ``placed`` report that counts for
    ``address`` (:func:`placed_section`), or None."""
    for item in sorted(items, key=lambda each: each.seq, reverse=True):
        event = item.event
        if event is not None and event.name == "placed" and event.signed is not None:
            section = placed_section(event.signed, sid, address, publication, checks)
            if section is not None:
                return section
    return None


# -- a correction ---------------------------------------------------------------

# The tags a correction writes itself; the published event's of these go.
_CORRECTED = frozenset({"d", "client", "title", "summary", "image", "published_at", "t",
                        "q", "imeta"})
_NEVER = frozenset({"expiration"})
# A public picture's address names its plain file's hash (BUD-01).
_PUBLIC_PICTURE = re.compile(r"https://[^\s)\"'<>]+?/([0-9a-f]{64})(?:\.[a-z0-9]{2,5})?")
_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^\s)>]+)")


def _language_tag(tag: List[str]) -> bool:
    return (tag[0] == "L" and tag[1:2] == [LANGUAGE_NAMESPACE]) or (
        tag[0] == "l" and len(tag) >= 3 and tag[2] == LANGUAGE_NAMESPACE)


def _tag(event: Mapping, name: str) -> str:
    return formats.tag_value(event, name) or ""


def corrected_template(revision: Mapping, published: Mapping, *, writer: str,
                       media_urls: Mapping[str, str], created_at: int) -> dict:
    """The kind 30023 template of the correction ``revision`` that
    ``writer`` signs: at the published event's ``d`` and first date, made
    after it, with the correction's text, title, summary, cover, topics
    and language, and every other tag of ``published`` as it stands."""
    if not formats.is_hex64(writer) or not isinstance(created_at, int):
        raise NotPublishable("invalid", "not a key or not a time")
    d = _tag(published, "d")
    if not d:
        raise NotPublishable("invalid", "the published article has no address")
    work = work_of(revision)
    media = [e for e in revision.get("media") or () if isinstance(e, Mapping)]
    content = _with_public_media(work["content"], media_urls)
    image = _with_public_media(work["image"], media_urls) if work["image"] else ""
    # The media tags follow the corrected text: a picture of its media[]
    # gets the imeta of its entry (5), one it removes loses it, one it
    # keeps keeps the published one; one per address.
    def shown(tag: List[str]) -> str:
        url = imeta_url(tag)
        return url if url and (url in content or url == image) else ""

    pictures = one_per_address(
        picture_imeta(media, media_urls)
        + [tag for tag in (_carried(tag, media_urls, media)
                           for tag in work["extra_tags"] if tag[0] == "imeta") if shown(tag)]
        + [list(tag) for tag in published.get("tags") or ()
           if _is_tag(tag) and tag[0] == "imeta" and shown(tag)])
    kept = [list(tag) for tag in published.get("tags") or ()
            if _is_tag(tag) and tag[0] not in _CORRECTED and tag[0] not in _NEVER
            and not _language_tag(tag)]
    event = publisher.build_article(
        content, writer, d, title=work["title"], summary=work["summary"], image=image,
        published_at=publisher.published_at_of(dict(published)) or created_at,
        hashtags=work["tags"], extra_tags=_language_tags(work["lang"]) + pictures + kept)
    before = published.get("created_at")
    event["created_at"] = before + 1 if isinstance(before, int) and created_at <= before \
        else created_at
    return event


def _plain_hashes(revision: Mapping) -> Mapping[str, str]:
    """``{ref: plain sha256}`` of the revision's media entries."""
    return {e["ref"]: e["plain_sha256"] for e in revision.get("media") or ()
            if isinstance(e, Mapping) and isinstance(e.get("ref"), str)
            and formats.is_hex64(e.get("plain_sha256"))}


def _same_cover(revision: Mapping, new: str, old: str) -> bool:
    if _MEDIA_REF.fullmatch(new):
        found = _PUBLIC_PICTURE.fullmatch(old)
        return found is not None and found.group(1) == _plain_hashes(revision).get(new)
    return new == old


def _pictures(text: str, hashes: Mapping[str, str]) -> List[str]:
    """The pictures ``text`` shows, in order and as often as it shows them
    (two that trade places are another text), each by its plain file's
    hash when known (a reference of the revision, or a public address),
    else by its address."""
    found = []
    for url in _IMAGE.findall(text):
        public = _PUBLIC_PICTURE.fullmatch(url)
        found.append(hashes.get(url, url) if _MEDIA_REF.fullmatch(url)
                     else public.group(1) if public else url)
    return found


def correction_changes(revision: Mapping, published: Mapping) -> List[Tuple[str, str]]:
    """What the correction ``revision`` changes besides the text, as
    ``(field, new value)``: ``title``, ``summary`` (``""``: it goes),
    ``cover`` (``added``, ``changed`` or ``removed``), ``topics`` (the
    new ones, ``#``-named and space-separated; ``""``: they go),
    ``pictures`` (the text's pictures are others, or in another order or
    number) and ``language`` (its code). [] for a correction that cannot
    be read."""
    try:
        work = work_of(revision)
    except NotPublishable:
        return []
    found: List[Tuple[str, str]] = []
    if work["title"] != _tag(published, "title").strip():
        found.append(("title", work["title"]))
    if work["summary"].strip() != _tag(published, "summary").strip():
        found.append(("summary", work["summary"].strip()))
    old_cover, new_cover = _tag(published, "image"), work["image"].strip()
    if new_cover and not old_cover:
        found.append(("cover", "added"))
    elif old_cover and not new_cover:
        found.append(("cover", "removed"))
    elif new_cover and not _same_cover(revision, new_cover, old_cover):
        found.append(("cover", "changed"))
    new_topics = list(dict.fromkeys(t.strip().lstrip("#").lower() for t in work["tags"]
                                    if t.strip().lstrip("#")))
    old_topics = list(dict.fromkeys(formats.tag_values(published, "t")))
    if set(new_topics) != set(old_topics):
        found.append(("topics", " ".join("#" + t for t in new_topics)))
    old = published.get("content") if isinstance(published.get("content"), str) else ""
    if _pictures(old, {}) != _pictures(work["content"], _plain_hashes(revision)):
        found.append(("pictures", ""))
    old_lang = next((tag[1] for tag in published.get("tags") or ()
                     if _is_tag(tag) and len(tag) >= 3 and tag[0] == "l"
                     and tag[2] == LANGUAGE_NAMESPACE), "")
    new_lang = (_language_tags(work["lang"]) or [[], ["", ""]])[1][1]
    if new_lang and new_lang != old_lang:
        found.append(("language", new_lang))
    return found


def correction_texts(revision: Mapping, published: Mapping) -> Tuple[str, str]:
    """The article as it stands in public and the correction's text, each
    with a picture both name written alike (its address differs: public
    there, encrypted here), so only what the correction changes differs."""
    try:
        new = work_of(revision)["content"]
    except NotPublishable:
        new = ""
    hashes = set(_plain_hashes(revision).values())
    old = published.get("content") if isinstance(published.get("content"), str) else ""
    old = _PUBLIC_PICTURE.sub(
        lambda match: "standup-media" if match.group(1) in hashes else match.group(0), old)
    return old.rstrip(), _MEDIA_REF.sub("standup-media", new).rstrip()


__all__ = ["Accepted", "Confirmation", "EXTENSIONS", "NEVER_CARRIED", "NotPublishable",
           "Published", "Release", "addresses_before", "corrected_template",
           "correction_changes", "correction_taken", "correction_texts", "credited",
           "credited_writer", "held_credit", "media_refs_in", "media_to_publish",
           "placed_section", "published_revision", "published_template", "published_work",
           "release", "rights_of", "section_of", "served_at", "to_confirm",
           "verified_accept", "verified_confirmations", "work_address", "work_of"]
