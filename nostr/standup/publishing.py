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
  ``published_at`` (``publisher.published_at_of``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .. import publisher, zap_shares
from ..article_details import AUTHOR_MARKER
from . import following as rules
from . import formats, views

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
    the id of the split it named, who decided, and its ``seq``."""

    revision: int
    split_rumor_id: Optional[str]
    actor: str
    seq: int


def verified_accept(items: Sequence[views.TimelineItem], sid: str,
                    deciders: Optional[Iterable[str]],
                    checks: Optional[rules.SignedChecks] = None) -> Optional[Accepted]:
    """The newest decision of ``items`` when it is an accept that verifies
    as one of ``deciders``, read from its signed body; else None (another
    decision came later, it does not verify, or the members are not
    known). Nothing that rests on the Redaktion's word takes any other."""
    decisions = [item for item in items
                 if item.event is not None and item.event.name in rules.DECISIONS]
    if not decisions or deciders is None:
        return None
    item = max(decisions, key=lambda each: each.seq)
    event = item.event
    if event.name != "accept" or not rules.action_verified(event, sid, tuple(deciders), checks):
        return None
    body = (checks or rules.SignedChecks()).body(event.signed,
                                                 path=views.action_path(event.name, sid),
                                           sid=sid) or {}
    revision, split = body.get("expected_revision"), body.get("split_rumor_id")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    return Accepted(revision=revision, split_rumor_id=split if isinstance(split, str) else None,
                    actor=event.actor or "", seq=item.seq)



def verified_published(items: Sequence[views.TimelineItem], sid: str, writer: str, d: str,
                       checks: Optional[rules.SignedChecks] = None) -> Optional[dict]:
    """The article as it stands in public, for a correction to replace
    (plan 8.6): the event of the newest ``published`` action the writer
    signed, when that event is the writer's kind 30023 at ``d``; else
    None. Never a server's word alone (12.4)."""
    for item in sorted(items, key=lambda each: each.seq, reverse=True):
        event = item.event
        if event is None or event.name != "published":
            continue
        if not rules.action_verified(event, sid, (writer,), checks):
            return None
        body = (checks or rules.SignedChecks()).body(
            event.signed, path=views.action_path("published", sid), sid=sid) or {}
        published = body.get("event")
        if (isinstance(published, dict) and published.get("kind") == 30023
                and published.get("pubkey") == writer
                and formats.tag_value(published, "d") == d):
            return published
        return None
    return None


__all__ = ["Accepted", "EXTENSIONS", "NEVER_CARRIED", "NotPublishable", "addresses_before",
           "credited",
           "media_refs_in", "media_to_publish", "published_template",
           "served_at", "verified_accept", "verified_published", "work_of"]
