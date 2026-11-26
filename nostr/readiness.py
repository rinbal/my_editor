# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Publication Check (DoktorShift/my_editor#33), without Qt.

Before an article goes out: what keeps it from publishing (blockers), what
would make it better (suggestions), and which groups were looked at and are
fine (checked). STANDUP owns the rules, thresholds and sentences
(shared/publication-check.json there, vendored here as
nostr/publication_check.json) and checks with src/utils/publicationCheck.js;
both apps run the same cases (tests/fixtures/publication_check_cases.json),
so a writer who uses both meets the same verdict in the same words. Change a
shared rule in STANDUP, never here.

The check reads the article as it would be built (``event``: kind, tags,
content), so it judges what readers would get, plus two facts only the
editor knows: whether an upload is still running, and whether the article is
scheduled (its future date is then intended).

MyEditor adds two blockers of its own, in its own words (gettext): a secret
key anywhere in the event, which no signer here signs, and an empty link
name, without which an article has no address.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from i18n import N_, _, language
from nostr.refs import secret_keys_in_event
from picture_names import description_of

NOSTR = "nostr"          # Ready to Publish
STANDUP = "standup"      # Ready for STANDUP: also what Discover asks
BLOCKER = "blocker"
SUGGESTION = "suggestion"

RULES: dict = json.loads(Path(__file__).with_name("publication_check.json").read_text(encoding="utf-8"))

# MyEditor's own findings, first: they stop signing or building outright.
SECRET_KEY = "secret.in_event"
NO_LINK_NAME = "slug.missing"
_OWN_SENTENCES = {
    SECRET_KEY: N_("The article contains a secret key. Remove it before publishing."),
    NO_LINK_NAME: N_("Give the article a link name before publishing."),
}
_OWN_GROUPS = {"secret": N_("Secret keys"), "link": N_("Link name")}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    field: str
    group: str
    params: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    outcome: str
    findings: Tuple[Finding, ...]
    checked: Tuple[str, ...]

    @property
    def blockers(self) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == BLOCKER)

    @property
    def suggestions(self) -> Tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SUGGESTION)

    @property
    def ready(self) -> bool:
        return not self.blockers


# --------------------------------------------------------------------------- #
# Reading the article                                                         #
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)[\s\S]*?\1")
_PICTURE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^\s)>]*)")
_TITLED_LINK = re.compile(r"\]\(\s*<?[^\s)>]*>?\s+(?:\"([^\"]*)\"|'([^']*)')\s*\)")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_ITALIC = re.compile(r"([*_])(?![*_\s])[\s\S]*\S\1")
_WEB_ADDRESS = re.compile(r"https?://", re.IGNORECASE)
_UNSAFE_CHARACTER = re.compile(r"[\s\x00-\x1f\x7f]")


def prose_of(markdown: str) -> str:
    """The text a reader reads: fenced code blocks and inline code left out."""
    lines: List[str] = []
    fence = ""
    for line in (markdown or "").split("\n"):
        marker = _FENCE.match(line.strip())
        if fence:
            if marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = ""
            lines.append("")
        elif marker:
            fence = marker.group(1)
            lines.append("")
        else:
            lines.append(_INLINE_CODE.sub("", line))
    return "\n".join(lines)


def escaped_size(value) -> int:
    """Bytes STANDUP's crawler counts for an event: compact JSON with every
    non-ASCII character escaped (relay_pool.py _event_byte_size there)."""
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def topic_key(raw: str, discover: Optional[dict] = None) -> str:
    """A hashtag as a Discover topic, or "" when Discover can't use it."""
    d = discover or RULES["limits"]["discover"]
    value = unicodedata.normalize("NFKC", str(raw)).strip().lower().removeprefix("#")
    if (not d["topic_min_chars"] <= len(value) <= d["topic_max_chars"]
            or value in d["topic_technical"]
            or not any(c.isalpha() for c in value)
            or any(c in value for c in d["topic_forbidden"])
            or value.startswith(d["topic_refused_prefix"])
            or re.fullmatch("[a-f0-9]{%d,}" % d["topic_hex_min_chars"], value)
            or not all(c.isalnum() or c in " -" for c in value)):
        return ""
    return value


def is_web_address(value: str) -> bool:
    """The cover rule both apps share: an http(s) address with a host, no
    user name or password, nothing that isn't printed."""
    if not _WEB_ADDRESS.match(value) or _UNSAFE_CHARACTER.search(value):
        return False
    try:
        parts = urlsplit(value)
        parts.port                              # a bad port raises
    except ValueError:
        return False
    return bool(parts.hostname) and parts.username is None and parts.password is None


@dataclass(frozen=True)
class _Article:
    title: str
    summary: str
    image: str
    published_at: str
    link_name: str
    hashtags: Tuple[str, ...]
    content: str
    prose: str
    unsigned: dict


def _tag_value(tags, name: str) -> str:
    for tag in tags:
        if isinstance(tag, (list, tuple)) and tag and tag[0] == name:
            return tag[1].strip() if len(tag) > 1 and isinstance(tag[1], str) else ""
    return ""


def _article_of(event: dict, now: int) -> _Article:
    tags = [t for t in event.get("tags") or () if isinstance(t, (list, tuple))]
    content = event.get("content") if isinstance(event.get("content"), str) else ""
    hashtags = tuple(t[1].strip() for t in tags
                     if len(t) > 1 and t[0] == "t" and isinstance(t[1], str) and t[1].strip())
    return _Article(
        title=_tag_value(tags, "title"), summary=_tag_value(tags, "summary"),
        image=_tag_value(tags, "image"), published_at=_tag_value(tags, "published_at"),
        link_name=_tag_value(tags, "d"), hashtags=hashtags, content=content,
        prose=prose_of(content),
        unsigned={"kind": event.get("kind", 30023), "created_at": event.get("created_at", now),
                  "tags": [list(t) for t in tags], "content": content})


def _undescribed_pictures(prose: str) -> Optional[int]:
    """How many pictures have no description, None when there are none.
    "No description" is the "No ALT" chip's rule (picture_names)."""
    pictures = _PICTURE.findall(prose)
    if not pictures:
        return None
    return sum(1 for alt, address in pictures if not description_of(alt, address))


def _werbelinks(prose: str, rules: dict) -> Tuple[int, bool]:
    """How many Werbelinks ``prose`` has, and whether its last paragraph
    (footnotes aside) says so: an italic line with a disclosure word."""
    titles = set(rules["werbelinks"]["titles"])
    count = sum(1 for first, second in _TITLED_LINK.findall(prose)
                if (first or second).strip().lower() in titles)
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK.split(prose)]
    paragraphs = [p for p in paragraphs if p and not p.startswith("[^")]
    last = paragraphs[-1] if paragraphs else ""
    italic = bool(_ITALIC.fullmatch(last)) and not re.match(r"([*_])\1", last)
    words = last.lower()
    return count, italic and any(w in words for w in rules["werbelinks"]["disclosure_words"])


# --------------------------------------------------------------------------- #
# The checks                                                                  #
# --------------------------------------------------------------------------- #
# Each answers True when it finds the problem (or the sentence's {count}),
# False when it looked and all is well, None when there was nothing to see.

def _discover(rules: dict) -> dict:
    return rules["limits"]["discover"]


def _title_short(a: _Article, facts, rules):
    if not a.title:
        return None
    d = _discover(rules)
    short = (len(a.title) < d["title_min_chars"] or a.title.lower() in d["untitled"]
             or bool(_WEB_ADDRESS.match(a.title)))
    return {"count": d["title_min_chars"]} if short else False


def _summary_short(a: _Article, facts, rules):
    if not a.summary:
        return None
    minimum = _discover(rules)["summary_min_chars"]
    return {"count": minimum} if len(a.summary) < minimum else False


def _body_short(a: _Article, facts, rules):
    if not a.content.strip():
        return None
    minimum = _discover(rules)["body_min_words"]
    return {"count": minimum} if len(a.content.split()) < minimum else False


def _too_big_for_standup(a: _Article, facts, rules):
    size = rules["limits"]["size"]
    return escaped_size(a.unsigned) + size["allowance_bytes"] > size["max_bytes"]


def _alt_missing(a: _Article, facts, rules):
    undescribed = _undescribed_pictures(a.prose)
    if undescribed is None:
        return None
    return {"count": undescribed} if undescribed else False


def _sponsored(a: _Article, facts, rules):
    count, disclosed = _werbelinks(a.prose, rules)
    return (not disclosed) if count else None


def _date_future(a: _Article, facts, rules):
    if not a.published_at:
        return None
    try:
        moment = float(a.published_at)
    except ValueError:
        return False                    # as STANDUP reads it: no number, no future
    return not facts["scheduled"] and moment > facts["now"]


_CHECKS: Dict[str, Callable] = {
    "title.missing": lambda a, facts, rules: not a.title,
    "body.empty": lambda a, facts, rules: not a.content.strip(),
    "media.uploading": lambda a, facts, rules: True if facts["uploading"] else None,
    "cover.unsafe": lambda a, facts, rules: (not facts["cover_ok"](a.image)) if a.image else None,
    "size.standup": _too_big_for_standup,
    "summary.missing": lambda a, facts, rules: not a.summary,
    "cover.missing": lambda a, facts, rules: not a.image,
    "tags.missing": lambda a, facts, rules: not a.hashtags,
    "media.alt_missing": _alt_missing,
    "date.future": _date_future,
    "sponsored.undisclosed": _sponsored,
    "discover.title_short": _title_short,
    "discover.summary_short": _summary_short,
    "discover.body_short": _body_short,
    "discover.no_topic": lambda a, facts, rules: (
        not any(topic_key(t, _discover(rules)) for t in a.hashtags) if a.hashtags else None),
}


def evaluate(event: dict, *, outcome: str = NOSTR, now: int, uploading: bool = False,
             scheduled: bool = False, rules: Optional[dict] = None,
             cover_ok: Callable[[str], bool] = is_web_address,
             own_checks: bool = True) -> Result:
    """The verdict for ``event`` (the article as it would be built) under
    ``outcome``. ``findings`` come most pressing first: MyEditor's own
    blockers, then the rule file's order. ``cover_ok`` may be stricter than
    the shared rule (the publisher's, which drops what it refuses).
    ``own_checks`` False leaves MyEditor's own findings out (the shared
    cases)."""
    rules = rules or RULES
    article = _article_of(event, now)
    facts = {"now": now, "uploading": uploading, "scheduled": scheduled, "cover_ok": cover_ok}
    findings: List[Finding] = []
    passed: List[str] = []
    raised = set()
    if own_checks:
        keys = secret_keys_in_event(article.unsigned)     # None: it can't be read, so no signer signs it
        for code, group, field_name, found in (
                (SECRET_KEY, "secret", "body", keys is None or bool(keys)),
                (NO_LINK_NAME, "link", "link_name", not article.link_name)):
            if found:
                findings.append(Finding(code, BLOCKER, field_name, group))
                raised.add(group)
            else:
                passed.append(group)
    for rule in rules["findings"]:
        severity = rule["severity"].get(outcome)
        check = _CHECKS.get(rule["code"])
        if not severity or check is None:          # a rule newer than this app: not ours to guess
            continue
        verdict = check(article, facts, rules)
        if verdict is None:
            continue
        if not verdict:
            passed.append(rule["group"])
            continue
        raised.add(rule["group"])
        findings.append(Finding(rule["code"], severity, rule["field"], rule["group"],
                                verdict if isinstance(verdict, dict) else {}))
    checked = tuple(dict.fromkeys(g for g in passed if g not in raised))
    return Result(outcome, tuple(findings), checked)


# --------------------------------------------------------------------------- #
# The words                                                                   #
# --------------------------------------------------------------------------- #

def sentence(key: str, params: Optional[Mapping] = None, *, lang: Optional[str] = None,
             rules: Optional[dict] = None) -> str:
    """A sentence of the rule file in the interface language (English when
    the file has none), its {count} filled in."""
    rules = rules or RULES
    params = params or {}
    english = rules["messages"]["en"]
    messages = rules["messages"].get(lang or language(), english)
    count = params.get("count")
    plural = None if count is None else f"{key}.{'one' if count == 1 else 'other'}"
    text = (plural and messages.get(plural)) or messages.get(key) or english.get(key) or key
    return re.sub(r"\{(\w+)\}", lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


def finding_sentence(finding: Finding, **kwargs) -> str:
    own = _OWN_SENTENCES.get(finding.code)
    return _(own) if own else sentence(finding.code, finding.params, **kwargs)


def group_label(group: str, **kwargs) -> str:
    own = _OWN_GROUPS.get(group)
    return _(own) if own else sentence(f"group.{group}", **kwargs)


def headline(result: Result, **kwargs) -> str:
    """The verdict in a few words: "Ready for STANDUP", or "2 things to fix"."""
    if result.blockers:
        return sentence("blockers", {"count": len(result.blockers)}, **kwargs)
    return sentence(f"ready.{result.outcome}", **kwargs)


def suggestions_line(result: Result, **kwargs) -> str:
    """ "2 suggestions", or "" when there are none."""
    count = len(result.suggestions)
    return sentence("suggestions", {"count": count}, **kwargs) if count else ""
