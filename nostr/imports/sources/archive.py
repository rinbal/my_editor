# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Platform export archives (ZIP): Medium and Substack.

These are the ``.zip`` a writer downloads when leaving a platform. Both
carry HTML post bodies; the structures match Ghost's official migrators
(@tryghost/mg-medium-export, @tryghost/mg-substack):

  Medium    ``posts/*.html`` with microformat classes: ``.p-name``
            (title), ``.e-content`` (body), ``.p-summary`` (subtitle),
            ``.dt-published[datetime]``, ``.p-canonical[href]``. Files
            whose basename starts with ``draft`` are drafts.
  Substack  ``posts.csv`` (post_id, post_date, is_published, ..., title,
            subtitle, podcast_url) joined by ``post_id`` to
            ``posts/<post_id>.html`` bodies.

Unzipping and CSV both come from the stdlib; Medium's microformats are
read with lxml (already a dependency via readability). Only *published*
posts are kept; bodies stay HTML for the pipeline's HTML-to-Markdown
pass.

Bounded: archives above ``MAX_ARCHIVE_BYTES`` and members above
``MAX_MEMBER_BYTES`` are rejected/skipped so a hostile ZIP cannot
exhaust memory (zip bombs decompress far larger than they store).
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import lxml.html

from ...rss.parser import FeedItem


MAX_ARCHIVE_BYTES = 256 * 1024 * 1024   # decompressed total across members
MAX_MEMBER_BYTES = 32 * 1024 * 1024     # any single member


class ArchiveError(Exception):
    """The archive can't be read (not a ZIP, or over the size bounds)."""


@dataclass(frozen=True)
class ArchiveResult:
    platform: Optional[str]  # "medium" | "substack" | None
    title: str
    items: Tuple[FeedItem, ...]


def looks_like_zip(data: bytes) -> bool:
    return isinstance(data, (bytes, bytearray)) and bytes(data[:4]) in (
        b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def _to_unix(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    raw = str(value).strip()
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def _read_member(zf: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        return None
    return zf.read(name)


def detect_archive(names: List[str]) -> Optional[str]:
    if any(n == "posts.csv" or n.endswith("/posts.csv") for n in names):
        return "substack"
    if any(re.search(r"(^|/)posts/.*\.html$", n, re.IGNORECASE) for n in names):
        return "medium"
    return None


# --------------------------------------------------------------------------- #
# Medium                                                                      #
# --------------------------------------------------------------------------- #

def _by_class(doc, class_name: str):
    matches = doc.xpath(
        "//*[contains(concat(' ', normalize-space(@class), ' '), "
        f"' {class_name} ')]"
    )
    return matches[0] if matches else None


def _inner_html(node) -> str:
    parts = [node.text or ""]
    for child in node:
        parts.append(lxml.html.tostring(child, encoding="unicode"))
    return "".join(parts).strip()


def _extract_medium(zf: zipfile.ZipFile) -> List[FeedItem]:
    items: List[FeedItem] = []
    for name in zf.namelist():
        if not re.search(r"(^|/)posts/.*\.html$", name, re.IGNORECASE):
            continue
        base = name.rsplit("/", 1)[-1]
        if base.lower().startswith("draft"):
            continue  # Medium drafts
        data = _read_member(zf, name)
        if data is None:
            continue
        try:
            doc = lxml.html.document_fromstring(
                data.decode("utf-8", errors="replace"))
        except (lxml.etree.ParserError, ValueError):
            continue
        content_node = _by_class(doc, "e-content")
        if content_node is None:
            continue
        content_html = _inner_html(content_node)
        if not content_html:
            continue
        title_node = _by_class(doc, "p-name")
        summary_node = _by_class(doc, "p-summary")
        published_node = _by_class(doc, "dt-published")
        canonical_node = _by_class(doc, "p-canonical")
        canonical = (
            canonical_node.get("href") if canonical_node is not None else "")
        items.append(FeedItem(
            guid=canonical or name,
            title=(title_node.text_content().strip()
                   if title_node is not None else "") or "Untitled",
            link=canonical or None,
            summary=(summary_node.text_content().strip()
                     if summary_node is not None else "") or None,
            content_html=content_html,
            published_at=_to_unix(
                published_node.get("datetime")
                if published_node is not None else None),
            categories=(),
            image=None,
            author=None,
        ))
    return items


# --------------------------------------------------------------------------- #
# Substack                                                                    #
# --------------------------------------------------------------------------- #

def _extract_substack(zf: zipfile.ZipFile) -> List[FeedItem]:
    csv_name = next(
        (n for n in zf.namelist()
         if n == "posts.csv" or n.endswith("/posts.csv")),
        None,
    )
    if csv_name is None:
        return []
    data = _read_member(zf, csv_name)
    if data is None:
        return []
    rows = list(csv.DictReader(
        io.StringIO(data.decode("utf-8", errors="replace"))))

    body_by_id = {}
    for name in zf.namelist():
        if not re.search(r"(^|/)posts/.*\.html$", name, re.IGNORECASE):
            continue
        post_id = name.rsplit("/", 1)[-1][: -len(".html")]
        body_by_id[post_id] = name

    items: List[FeedItem] = []
    for row in rows:
        if str(row.get("is_published", "")).upper() != "TRUE":
            continue
        post_id = str(row.get("post_id") or "")
        member = body_by_id.get(post_id)
        content_html = ""
        if member:
            data = _read_member(zf, member)
            if data is not None:
                content_html = data.decode("utf-8", errors="replace").strip()
        if not content_html and not row.get("podcast_url"):
            continue
        items.append(FeedItem(
            guid=f"substack-{post_id}",
            title=str(row.get("title") or "Untitled").strip(),
            link=None,
            summary=str(row.get("subtitle") or "").strip() or None,
            content_html=content_html,
            published_at=_to_unix(row.get("post_date")),
            categories=(str(row["type"]),) if row.get("type") else (),
            image=None,
            author=None,
        ))
    return items


# --------------------------------------------------------------------------- #
# Entry                                                                       #
# --------------------------------------------------------------------------- #

def extract_archive(data: bytes) -> ArchiveResult:
    """Unzip and extract a platform export archive.

    Raises :class:`ArchiveError` when the bytes aren't a readable ZIP
    or blow the size bounds; returns ``platform=None`` when the ZIP is
    valid but matches no known export layout.
    """
    if not looks_like_zip(data):
        raise ArchiveError("Not a ZIP archive")
    try:
        zf = zipfile.ZipFile(io.BytesIO(bytes(data)))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"Could not read that archive: {exc}") from exc
    with zf:
        total = sum(info.file_size for info in zf.infolist())
        if total > MAX_ARCHIVE_BYTES:
            raise ArchiveError("That archive is too large to import")
        platform = detect_archive(zf.namelist())
        if platform is None:
            return ArchiveResult(platform=None, title="", items=())
        items = (
            _extract_substack(zf) if platform == "substack"
            else _extract_medium(zf)
        )
        title = "Substack export" if platform == "substack" else "Medium export"
        return ArchiveResult(
            platform=platform, title=title, items=tuple(items))
