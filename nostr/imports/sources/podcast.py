# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Podcasting 2.0 support for the RSS import path.

A podcast feed IS an RSS feed, so it flows through the normal RSS
resolver. What the shared feed parser drops on the floor is exactly
what makes it a podcast: the ``<enclosure>`` audio file and the
``podcast:`` / ``itunes:`` namespace tags (duration, chapters,
transcript, value splits). This module recovers those with a
CDATA-aware regex pass over the raw XML and attaches them to the
parsed feed's items.

Split of responsibilities:

- :func:`enrich_feed_with_podcast` runs at PREVIEW time in the RSS
  resolver. Cheap, no network; a non-podcast feed passes through
  untouched.
- :func:`fetch_podcast_chapters` runs at IMPORT time, for selected
  items only: the external chapters JSON (podcast-namespace
  jsonChapters) is fetched and flattened. Best-effort; a dead chapters
  URL never fails an import.
- :func:`build_podcast_markdown` renders the episode as Markdown
  (audio link, metadata line, chapter list, transcript link, show
  notes). Deliberate divergence from the reference importer, which
  serialises a platform-specific structured format its published site
  renders; this editor has no such renderer, so Markdown is the
  faithful representation until one exists.

Fact notes (podcastindex.org namespace, verified against live feeds):
``itunes:duration`` is bare seconds or HH:MM:SS; ``podcast:value``
recipients flagged ``fee="true"`` are off-the-top infrastructure fees,
not proportional splits, and are excluded rather than misrepresented;
chapters entries with ``toc: false`` are hidden from listings by spec
and are skipped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Callable, List, Optional, Tuple

from ...rss.parser import Feed, FeedItem


# Defensive caps so a hostile feed can't bloat the draft event.
MAX_CHAPTERS = 100
MAX_RECIPIENTS = 10

_ITEM_RE = re.compile(r"<item[\s>][\s\S]*?</item>", re.IGNORECASE)
_CDATA_RE = re.compile(r"^<!\[CDATA\[([\s\S]*?)\]\]>$")
_ENCLOSURE_RE = re.compile(r"<enclosure\s[^>]*/?>", re.IGNORECASE)
_TRANSCRIPT_RE = re.compile(r"<podcast:transcript\s[^>]*/?>", re.IGNORECASE)
_CHAPTERS_RE = re.compile(r"<podcast:chapters\s[^>]*/?>", re.IGNORECASE)
_VALUE_BLOCK_RE = re.compile(
    r"<podcast:value[\s>][\s\S]*?</podcast:value>", re.IGNORECASE)
_RECIPIENT_RE = re.compile(
    r"<podcast:valueRecipient\s[^>]*/?>", re.IGNORECASE)

_AUDIO_EXT_RE = re.compile(
    r"\.(mp3|m4a|aac|ogg|oga|opus|flac|wav|weba)(?:[?#]|$)", re.IGNORECASE)

# Preferred transcript order: renderable-in-page formats first.
_TRANSCRIPT_PREFERENCE = (
    "text/vtt", "application/x-subrip", "application/srt",
    "application/json", "text/plain",
)


@dataclass(frozen=True)
class ValueRecipient:
    name: str
    type: str
    address: str
    split: int


@dataclass(frozen=True)
class PodcastEpisode:
    audio: str
    audio_type: str = ""
    audio_bytes: int = 0
    duration: int = 0
    show: str = ""
    episode: int = 0
    season: int = 0
    medium: str = "podcast"
    feed_guid: str = ""
    item_guid: str = ""
    chapters_url: str = ""
    transcript: str = ""
    transcript_type: str = ""
    value: Tuple[ValueRecipient, ...] = ()


@dataclass(frozen=True)
class Chapter:
    start: int
    title: str


# --------------------------------------------------------------------------- #
# Raw-XML extraction helpers                                                  #
# --------------------------------------------------------------------------- #

def _decode_entities(value: str) -> str:
    return (
        str(value)
        .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#039;", "'").replace("&apos;", "'")
    )


def _tag_text(xml: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}(?:\s[^>]*)?>([\s\S]*?)</{tag}>", xml, re.IGNORECASE)
    if not match:
        return ""
    inner = match.group(1).strip()
    cdata = _CDATA_RE.match(inner)
    return _decode_entities((cdata.group(1) if cdata else inner).strip())


def _attr_of(element_xml: str, attr: str) -> str:
    match = (
        re.search(rf'\b{attr}\s*=\s*"([^"]*)"', element_xml, re.IGNORECASE)
        or re.search(rf"\b{attr}\s*=\s*'([^']*)'", element_xml, re.IGNORECASE)
    )
    return _decode_entities(match.group(1).strip()) if match else ""


def _tag_attr(xml: str, tag: str, attr: str) -> str:
    match = re.search(rf"<{tag}\s[^>]*>", xml, re.IGNORECASE)
    return _attr_of(match.group(0), attr) if match else ""


def normalize_duration(raw: str) -> int:
    """``"3480"`` | ``"58:00"`` | ``"1:02:33"`` -> seconds; 0 if unusable."""
    value = str(raw or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    parts = value.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    if any(n < 0 for n in numbers):
        return 0
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    return 0


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _is_audio_enclosure(url: str, mime: str) -> bool:
    if mime and mime.lower().startswith("audio/"):
        return True
    if mime:
        return False  # explicit non-audio mime (video podcast etc.)
    return bool(_AUDIO_EXT_RE.search(url))


def _pick_transcript(item_xml: str) -> Tuple[str, str]:
    candidates = [
        (_attr_of(el, "url"), _attr_of(el, "type").lower())
        for el in _TRANSCRIPT_RE.findall(item_xml)
    ]
    candidates = [
        (url, mime) for url, mime in candidates
        if url.lower().startswith(("http://", "https://"))
    ]
    if not candidates:
        return "", ""
    for preferred in _TRANSCRIPT_PREFERENCE:
        for url, mime in candidates:
            if mime == preferred:
                return url, mime
    return candidates[0]


def _parse_value_block(xml: str) -> Tuple[ValueRecipient, ...]:
    block = _VALUE_BLOCK_RE.search(xml)
    if not block:
        return ()
    out: List[ValueRecipient] = []
    for element in _RECIPIENT_RE.findall(block.group(0)):
        if _attr_of(element, "fee").lower() == "true":
            continue  # off-the-top fee, not a proportional split
        address = _attr_of(element, "address")
        try:
            split = int(float(_attr_of(element, "split") or "0"))
        except ValueError:
            continue
        if not address or split <= 0:
            continue
        out.append(ValueRecipient(
            name=_attr_of(element, "name") or address[:12],
            type=(_attr_of(element, "type") or "node").lower(),
            address=address,
            split=split,
        ))
        if len(out) >= MAX_RECIPIENTS:
            break
    return tuple(out)


def looks_like_podcast_feed(xml: Optional[str]) -> bool:
    """XML with at least one audio enclosure. Cheap enough to run on
    every parsed RSS body."""
    if not isinstance(xml, str) or "<enclosure" not in xml:
        return False
    return any(
        _is_audio_enclosure(_attr_of(el, "url"), _attr_of(el, "type"))
        for el in _ENCLOSURE_RE.findall(xml)
    )


def extract_podcast_feed(xml: str) -> Tuple[dict, List[dict]]:
    """Per-item podcast data + channel-level defaults from raw feed XML.

    Only items with an audio enclosure are included.
    """
    first_item = _ITEM_RE.search(xml)
    channel_xml = xml[: first_item.start()] if first_item else xml
    channel = {
        "value": _parse_value_block(channel_xml),
        "image": _tag_attr(channel_xml, "itunes:image", "href"),
        "medium": _tag_text(channel_xml, "podcast:medium") or "podcast",
        "title": _tag_text(channel_xml, "title"),
        # The show's permanent identity (UUIDv5 of the feed URL).
        "guid": _tag_text(channel_xml, "podcast:guid"),
    }

    items: List[dict] = []
    for item_match in _ITEM_RE.finditer(xml):
        item_xml = item_match.group(0)
        audio = audio_type = ""
        audio_bytes = 0
        for element in _ENCLOSURE_RE.findall(item_xml):
            url = _attr_of(element, "url")
            mime = _attr_of(element, "type")
            if _is_audio_enclosure(url, mime) and \
                    url.lower().startswith(("http://", "https://")):
                audio, audio_type = url, mime
                try:
                    audio_bytes = int(_attr_of(element, "length") or "0")
                except ValueError:
                    audio_bytes = 0
                break
        if not audio:
            continue  # not an audio episode (blog item, video-only, ...)

        transcript_url, transcript_type = _pick_transcript(item_xml)
        chapters_el = _CHAPTERS_RE.search(item_xml)
        chapters_url = _attr_of(chapters_el.group(0), "url") \
            if chapters_el else ""
        if not chapters_url.lower().startswith(("http://", "https://")):
            chapters_url = ""

        def _int_tag(name: str) -> int:
            try:
                return int(_tag_text(item_xml, name) or "0")
            except ValueError:
                return 0

        items.append({
            "guid": _tag_text(item_xml, "guid"),
            "link": _tag_text(item_xml, "link"),
            "title": _tag_text(item_xml, "title"),
            "audio": audio,
            "audio_type": audio_type,
            "audio_bytes": audio_bytes,
            "duration": normalize_duration(_tag_text(item_xml, "itunes:duration")),
            "episode": _int_tag("itunes:episode"),
            "season": _int_tag("itunes:season"),
            "chapters_url": chapters_url,
            "transcript": transcript_url,
            "transcript_type": transcript_type,
            "value": _parse_value_block(item_xml),
            "image": _tag_attr(item_xml, "itunes:image", "href"),
        })
    return channel, items


def enrich_feed_with_podcast(feed: Feed, xml_body: str) -> Feed:
    """Attach podcast payloads to a parsed feed's items.

    Items are matched by guid, then link, then title. Non-podcast
    bodies pass through untouched. Episode art (or the show cover)
    becomes the article hero image when the item has none.
    """
    if not feed.items or not looks_like_podcast_feed(xml_body):
        return feed
    channel, episodes = extract_podcast_feed(xml_body)
    if not episodes:
        return feed

    by_guid = {e["guid"]: e for e in episodes if e["guid"]}
    by_link = {e["link"]: e for e in episodes if e["link"]}
    by_title = {e["title"]: e for e in episodes if e["title"]}

    updated: List[FeedItem] = []
    matched = False
    for item in feed.items:
        episode = (
            (item.guid and by_guid.get(item.guid))
            or (item.link and by_link.get(item.link))
            or (item.title and by_title.get(item.title))
        )
        if not episode:
            updated.append(item)
            continue
        matched = True
        payload = PodcastEpisode(
            audio=episode["audio"],
            audio_type=episode["audio_type"],
            audio_bytes=episode["audio_bytes"],
            duration=episode["duration"],
            show=channel["title"],
            episode=episode["episode"],
            season=episode["season"],
            medium=channel["medium"],
            feed_guid=channel["guid"],
            item_guid=episode["guid"],
            chapters_url=episode["chapters_url"],
            transcript=episode["transcript"],
            transcript_type=episode["transcript_type"],
            # Item-level value overrides channel-level, per spec.
            value=episode["value"] or channel["value"],
        )
        updated.append(replace(
            item,
            podcast=payload,
            image=item.image or episode["image"] or channel["image"] or None,
        ))
    if not matched:
        return feed
    return replace(feed, items=tuple(updated))


# --------------------------------------------------------------------------- #
# Import-time: chapters + Markdown serialisation                              #
# --------------------------------------------------------------------------- #

def parse_chapters_json(body: str) -> List[Chapter]:
    """Flatten podcast-namespace jsonChapters. Never throws."""
    try:
        payload = json.loads(body or "")
    except (ValueError, TypeError):
        return []
    rows = payload.get("chapters") if isinstance(payload, dict) else None
    chapters: List[Chapter] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        if row.get("toc") is False:
            continue  # spec: toc:false chapters stay off listings
        try:
            start = int(float(row.get("startTime")))
        except (TypeError, ValueError):
            continue
        if start < 0:
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        chapters.append(Chapter(start=start, title=title))
    chapters.sort(key=lambda c: c.start)
    return chapters[:MAX_CHAPTERS]


def fetch_podcast_chapters(
    fetcher,
    url: str,
    on_done: Callable[[List[Chapter]], None],
) -> None:
    """Fetch + flatten the external chapters JSON; best-effort.

    Real feeds route chapters through analytics prefix services that
    embed the real file URL in the path
    (``https://wrapper.example/.../https://host/ch.json``). When the
    wrapper does not respond usefully, we retry once against the
    embedded target instead of silently losing the chapters.
    """
    if not url:
        on_done([])
        return

    def _retry_embedded() -> None:
        embedded = re.search(r"/(https?://.+)$", url or "")
        target = embedded.group(1) if embedded else ""
        if not target or target == url:
            on_done([])
            return
        fetcher.fetch(
            target,
            on_success=lambda body: on_done(parse_chapters_json(body)),
            on_failure=lambda _err: on_done([]),
        )

    def _on_body(body: str) -> None:
        chapters = parse_chapters_json(body)
        if chapters:
            on_done(chapters)
        else:
            _retry_embedded()

    fetcher.fetch(url, on_success=_on_body, on_failure=lambda _e: _retry_embedded())


def build_podcast_markdown(
    episode: PodcastEpisode,
    *,
    chapters: List[Chapter] = (),
    notes: str = "",
) -> str:
    """Render an episode as a Markdown article body.

    Documented divergence from the reference importer (which emits a
    platform-rendered structured format): audio link, metadata line,
    chapter list with timestamps, transcript link, then the show notes.
    Value splits are parsed for completeness but not rendered; a draft
    has no meaningful representation for payment routing.
    """
    lines: List[str] = []
    meta_parts = []
    if episode.show:
        meta_parts.append(f"**{episode.show}**")
    if episode.season:
        meta_parts.append(f"Season {episode.season}")
    if episode.episode:
        meta_parts.append(f"Episode {episode.episode}")
    if episode.duration:
        meta_parts.append(format_duration(episode.duration))
    if meta_parts:
        lines.append("🎧 " + " · ".join(meta_parts))
        lines.append("")
    lines.append(f"[Listen to the episode]({episode.audio})")
    if chapters:
        lines.append("")
        lines.append("## Chapters")
        lines.append("")
        for chapter in chapters:
            lines.append(
                f"- **{format_duration(chapter.start)}** {chapter.title}")
    if episode.transcript:
        lines.append("")
        lines.append(f"[Transcript]({episode.transcript})")
    if notes.strip():
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(notes.strip())
    return "\n".join(lines).strip()
