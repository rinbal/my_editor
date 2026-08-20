# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""MDX / Markdown source resolver.

Two shapes, both landing on the same normalised ``Feed``:

- a single ``.md`` / ``.mdx`` document URL (raw, or a GitHub blob URL
  which is rewritten to its raw form for fetching): one draft;
- a GitHub *folder* URL (``github.com/owner/repo/tree/branch/path``):
  one draft per ``.md`` / ``.mdx`` file in that folder, fetched with
  bounded concurrency, so a whole content section imports in one pass
  and the preview list lets the user deselect any they don't want.

Each document is converted by ``mdx_to_markdown`` on a worker thread
(via the context's blocking executor) and carried as
``content_markdown`` so the import pipeline takes it verbatim.
"""

from __future__ import annotations

import json
import re
from typing import Callable, List, Optional
from urllib.parse import quote, urlparse

from ...rss.parser import Feed, FeedItem
from ..errors import ERROR_CODES, SourceError
from ..registry import ResolveContext, ResolveInput, ResolveResult, SourceResolver
from ..sources.mdx import mdx_to_markdown


# Files pulled from one folder: high enough to be "all of it" for real
# content sections, a guard against pointing at a pathological megafolder.
MDX_FOLDER_MAX = 100
# Parallel document fetches: quick without hammering the raw host.
MDX_FETCH_CONCURRENCY = 6

_MD_EXT_RE = re.compile(r"\.mdx?(?:$|[?#])", re.IGNORECASE)
_GH_TREE_RE = re.compile(
    r"^(?:https?://)?github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+?)/?$",
    re.IGNORECASE,
)
_GH_BLOB_RE = re.compile(
    r"^(?:https?://)?github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
    re.IGNORECASE,
)


def _pathname(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return parsed.path or ""
    except ValueError:
        return ""


def _hostname(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        return (parsed.hostname or "").removeprefix("www.")
    except ValueError:
        return ""


def _basename(url: str) -> str:
    segments = [s for s in _pathname(url).split("/") if s]
    return segments[-1] if segments else ""


def parse_github_tree(url: str):
    """``github.com/o/r/tree/b/path`` -> (owner, repo, branch, path)."""
    match = _GH_TREE_RE.match(str(url or "").strip())
    if not match:
        return None
    return (match.group(1), match.group(2), match.group(3),
            match.group(4).rstrip("/"))


def _to_raw_github(url: str) -> Optional[str]:
    """``github.com/o/r/blob/b/path`` -> its raw.githubusercontent URL."""
    match = _GH_BLOB_RE.match(str(url or "").strip())
    if not match:
        return None
    return (
        "https://raw.githubusercontent.com/"
        f"{match.group(1)}/{match.group(2)}/{match.group(3)}/{match.group(4)}"
    )


def _pretty_name(name: str) -> str:
    name = _MD_EXT_RE.sub("", str(name or ""))
    name = re.sub(r"^\d+[-_]", "", name)   # leading "01-" ordering prefix
    return re.sub(r"[-_]+", " ", name).strip()


def _detect(input_: ResolveInput) -> bool:
    url = input_.url
    if not url:
        return False
    if _GH_TREE_RE.match(url.strip()):
        return True
    return bool(_MD_EXT_RE.search(_pathname(url)))


def _doc_to_item(raw: str, *, fallback_name: str, link: str, guid: str) -> FeedItem:
    doc = mdx_to_markdown(raw or "")
    return FeedItem(
        guid=guid or link,
        title=doc.title or _pretty_name(fallback_name),
        link=link,
        summary=doc.summary or None,
        content_html="",
        published_at=None,
        categories=(),
        image=None,
        author=None,
        content_markdown=doc.markdown,
    )


# --------------------------------------------------------------------------- #
# Bounded-concurrency fan-out over the async fetcher                          #
# --------------------------------------------------------------------------- #

def _fetch_many(
    urls: List[str],
    fetcher,
    *,
    limit: int,
    on_done: Callable[[List[Optional[str]]], None],
    is_cancelled: Callable[[], bool],
) -> None:
    """Fetch every URL with at most ``limit`` in flight; failures yield
    ``None`` in that slot so one bad file never sinks the folder."""
    if not urls:
        on_done([])
        return
    results: List[Optional[str]] = [None] * len(urls)
    state = {"next": 0, "active": 0, "settled": 0}

    def _pump() -> None:
        while (
            state["next"] < len(urls)
            and state["active"] < limit
            and not is_cancelled()
        ):
            index = state["next"]
            state["next"] += 1
            state["active"] += 1
            fetcher.fetch(
                urls[index],
                on_success=lambda body, i=index: _settle(i, body),
                on_failure=lambda _err, i=index: _settle(i, None),
            )
        if is_cancelled() and state["active"] == 0 \
                and state["settled"] < len(urls):
            # Cancellation drained the queue; deliver what we have.
            state["settled"] = len(urls)
            on_done(results)

    def _settle(index: int, body: Optional[str]) -> None:
        if state["settled"] >= len(urls):
            return
        results[index] = body
        state["active"] -= 1
        state["settled"] += 1
        if state["settled"] == len(urls):
            on_done(results)
        else:
            _pump()

    _pump()


# --------------------------------------------------------------------------- #
# Resolve                                                                     #
# --------------------------------------------------------------------------- #

def _resolve(input_: ResolveInput, ctx: ResolveContext) -> None:
    url = (input_.url or "").strip()
    tree = parse_github_tree(url)
    if tree is not None:
        _resolve_folder(url, tree, ctx)
    else:
        _resolve_single(url, ctx)


def _resolve_single(url: str, ctx: ResolveContext) -> None:
    host = _hostname(url)
    ctx.stage("connecting", url, hostname=host)
    fetch_url = _to_raw_github(url) or url

    def _on_body(body: str) -> None:
        if ctx.is_cancelled():
            return
        ctx.stage("parsing", url, hostname=host)
        ctx.blocking(
            lambda: _doc_to_item(
                body, fallback_name=_basename(url), link=url, guid=url),
            lambda item: _deliver(item),
            lambda exc: ctx.on_failure(SourceError(
                f"Could not read that document: {exc}", ERROR_CODES.UNKNOWN)),
        )

    def _deliver(item: FeedItem) -> None:
        if ctx.is_cancelled():
            return
        feed = Feed(format="mdx", title=item.title, link=url,
                    description=None, items=(item,))
        ctx.stage("done", url, hostname=host, item_count=1)
        ctx.on_success(ResolveResult(url=url, feed=feed))

    ctx.fetcher.fetch(fetch_url, on_success=_on_body,
                      on_failure=ctx.on_failure)


def _resolve_folder(url: str, tree, ctx: ResolveContext) -> None:
    owner, repo, branch, path = tree
    ctx.stage("connecting", url, hostname="github.com")
    encoded_path = "/".join(quote(seg) for seg in path.split("/"))
    api_url = (
        f"https://api.github.com/repos/{owner}/{repo}/contents/"
        f"{encoded_path}?ref={quote(branch)}"
    )

    def _on_listing(body: str) -> None:
        if ctx.is_cancelled():
            return
        try:
            entries = json.loads(body)
        except (ValueError, TypeError):
            ctx.on_failure(SourceError(
                "GitHub returned an unexpected folder listing",
                ERROR_CODES.SOURCE_ERROR,
            ))
            return
        if not isinstance(entries, list):
            message = (
                f"GitHub: {entries.get('message')}"
                if isinstance(entries, dict) and entries.get("message")
                else "That GitHub path is not a folder"
            )
            ctx.on_failure(SourceError(message, ERROR_CODES.SOURCE_ERROR))
            return

        files = [
            e for e in entries
            if isinstance(e, dict) and e.get("type") == "file"
            and _MD_EXT_RE.search(str(e.get("name") or ""))
        ]
        files.sort(key=lambda e: _numeric_sort_key(str(e.get("name") or "")))
        if not files:
            ctx.on_failure(SourceError(
                "No .md or .mdx files in that folder",
                ERROR_CODES.SOURCE_ERROR,
            ))
            return
        total = len(files)
        files = files[:MDX_FOLDER_MAX]

        ctx.stage("parsing", url, hostname="github.com")
        _fetch_many(
            [str(f.get("download_url") or "") for f in files],
            ctx.fetcher,
            limit=MDX_FETCH_CONCURRENCY,
            on_done=lambda bodies: _convert(files, bodies, total),
            is_cancelled=ctx.is_cancelled,
        )

    def _convert(files: list, bodies: List[Optional[str]], total: int) -> None:
        if ctx.is_cancelled():
            return

        def _build() -> List[FeedItem]:
            items: List[FeedItem] = []
            for meta, body in zip(files, bodies):
                if body is None:
                    continue  # one bad file must not sink the folder
                try:
                    items.append(_doc_to_item(
                        body,
                        fallback_name=str(meta.get("name") or ""),
                        link=str(meta.get("html_url")
                                 or meta.get("download_url") or ""),
                        guid=str(meta.get("path")
                                 or meta.get("download_url") or ""),
                    ))
                except Exception:  # noqa: BLE001, ditto
                    continue
            return items

        ctx.blocking(
            _build,
            lambda items: _deliver(items, total),
            lambda exc: ctx.on_failure(SourceError(
                f"Could not convert that folder: {exc}", ERROR_CODES.UNKNOWN)),
        )

    def _deliver(items: List[FeedItem], total: int) -> None:
        if ctx.is_cancelled():
            return
        folder_name = path.split("/")[-1] if path else repo
        feed = Feed(
            format="mdx",
            title=f"{repo}/{folder_name}",
            link=url,
            # Surface the cap rather than silently importing a subset.
            description=(
                f"Showing the first {MDX_FOLDER_MAX} of {total} files"
                if total > MDX_FOLDER_MAX else None
            ),
            items=tuple(items),
        )
        ctx.stage("done", url, hostname="github.com", item_count=len(items))
        ctx.on_success(ResolveResult(url=url, feed=feed))

    ctx.fetcher.fetch(api_url, on_success=_on_listing,
                      on_failure=ctx.on_failure)


def _numeric_sort_key(name: str):
    """Name-aware numeric ordering: 2-intro sorts before 10-outro."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    ]


MDX_RESOLVER = SourceResolver(
    id="mdx",
    label="MDX / Markdown",
    detect=_detect,
    resolve=_resolve,
)
