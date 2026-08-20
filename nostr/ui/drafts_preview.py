# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hover preview for one draft: a popover, its geometry and its timing.

Resting the pointer on a drafts-panel row for half a second opens a
floating preview of that draft. It is a **popover**, not a tooltip:
``offering-help.md`` limits a tooltip to "a maximum of 60 to 75
characters", which a hero image plus a summary plus three metadata lines
is nowhere near. ``popovers.md`` endorses the surface instead: "Consider
using popovers when you want more room for content. Views like sidebars
and panels take up a lot of space. If you need content only temporarily,
displaying it in a popover can help streamline your interface."

It lives in its own module for two reasons. The panel is already large,
and the popover has to be renderable from a ``DraftRecord`` alone so it
can be composed, measured and screenshotted in a test with no panel, no
store and no screen, the way ``tests/test_drafts_panel.py`` renders a
single row.

The one rule the whole design is built around is ``accessibility.md``:
"Minimize use of time-boxed interface elements. Views and controls that
auto-dismiss on a timer can be problematic for people who need longer to
process information, and for people who use assistive technologies that
require more time to traverse the interface. Prefer dismissing views
with an explicit action." A hover popover is time-boxed by construction,
so it is never the only route to anything it shows. Space on the focused
row opens the same surface with the keyboard and gives it focus, the
context menu carries Show Preview so the key is discoverable, and
:func:`preview_announcement` puts the same facts on the row itself for a
screen reader that never opens a popover at all.

Three seams keep this testable and network-free:

  - the placement rules are :func:`preview_geometry`, which takes the
    available rect as a plain ``QRect`` argument, so every flip, clamp
    and multi-monitor case is a table-driven unit test;
  - the content rules are :func:`preview_fields`, a pure record to
    strings function;
  - the hero image arrives through an injected loader, and a panel with
    no loader injected renders no hero image and constructs no request,
    ever.
"""

from __future__ import annotations

import math
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import url_safety

from ..draft_store import DraftRecord, DraftState
from ..drafts import flatten_markdown_inline
from .drafts_common import (
    BASE_POINT_SIZE,
    THEME_TOKENS,
    app_point_size,
    first_tag,
    format_absolute_date,
    format_absolute_time,
    scaled,
    secondary_font,
    source_host,
)


# --------------------------------------------------------------------------- #
# Timing                                                                      #
# --------------------------------------------------------------------------- #

# Module level, and read at call time rather than captured, so a test can
# drive them to zero and fire the timers directly instead of sleeping.

# The pointer rests on a row and nothing is open. Long, because the panel
# is a scrolling list of narrow rows at the right edge of the window and
# almost every pointer journey to the scrollbar, the close button or the
# editor crosses several of them. ``pointing-devices.md``: "Avoid
# creating gratuitous pointer and content effects. People notice when the
# appearance of the pointer or the UI element beneath it changes, and
# they expect the changes to be useful."
PREVIEW_OPEN_DELAY_MS: int = 500

# Row to row while a popover is already open. Comparing two drafts is the
# most likely reason to open this twice, so charging the full dwell again
# per row would make the feature read as broken; zero would render a
# preview and start an image fetch for every row a drag passes over.
PREVIEW_SWAP_DELAY_MS: int = 120

# The pointer left the row but may be travelling to the popover, which
# sits across the panel's border and an 8 px gap. Without this the window
# closes in the gap between the thing that opened it and itself.
PREVIEW_LEAVE_GRACE_MS: int = 200

# The pointer left the popover itself. Shorter, because leaving is
# deliberate; long enough that a jitter at its own edge while reading
# does not close it.
PREVIEW_CLOSE_DELAY_MS: int = 150

# Opacity fade on open and close, and the duration of the animated
# geometry change on a swap. ``accessibility.md`` lists "Replacing
# transitions in x-, y-, and z-axes with fades to avoid motion" among its
# reduce-motion techniques, so a pure fade is the form the HIG already
# recommends as the reduced substitute and needs no platform query, which
# is just as well because Qt exposes none.
PREVIEW_FADE_MS: int = 120

# How long the reserved image box stays silent before it says it is
# waiting. ``loading.md`` asks apps to "Clearly communicate that content
# is loading and how long it might take to complete", and a label that
# appears instantly and vanishes 90 ms later on a warm cache is flicker
# rather than communication.
PREVIEW_IMAGE_SLOW_MS: int = 800

# Distance from the anchor row to the popover's visible edge.
PREVIEW_GAP_PX: int = 8


# --------------------------------------------------------------------------- #
# Box model                                                                   #
# --------------------------------------------------------------------------- #

# Content width at the macOS 13 pt default. 360 px with 14 px padding
# leaves 332 px of text, roughly 55 to 60 characters at 13 pt, which is
# inside the comfortable measure and is wider than the panel is at
# MIN_PANEL_WIDTH, which is the point of the surface. ``popovers.md``:
# "Avoid making a popover too big. Make a popover only big enough to
# display its contents and point to the place it came from."
_BASE_WIDTH: int = 360
_MIN_WIDTH: int = 320
_MAX_WIDTH: int = 460
_BASE_MAX_HEIGHT: int = 460

# 14 px on all four sides, more than the panel's 12 px GUTTER. A floating
# surface needs more air than an inline row, and the popover is not
# aligned to the panel's gutter anyway, so matching it would be a
# coincidence rather than an alignment.
_PAD: int = 14
_RADIUS: int = 8

# The arrow, required by ``popovers.md``: "Make sure a popover's arrow
# points as directly as possible to the element that revealed it." It
# does a second job here: hovering never changes the list's selection, so
# the previewed row and the selected row are usually different rows, and
# the arrow is what says which one the preview is about.
_ARROW_BASE: int = 12
_ARROW_DEPTH: int = 7
# Keeps the arrow's base off the corner radius. When the clamp has pushed
# the popover far enough that the tip would leave this band the arrow is
# dropped entirely: a stub pointing at nothing is worse than no arrow.
_ARROW_INSET: int = 16

# Transparent margin around the surface, holding the drop shadow. The
# shadow is what separates the popover from arbitrary editor content
# behind it, which no border token can do on its own.
_SHADOW_MARGIN: int = 24
_SHADOW_BLUR: float = 24.0
_SHADOW_DY: int = 4

# Clearance from the edges of the screen's available rect.
_SCREEN_MARGIN: int = 8

# Vertical rhythm. The 3 px inside the metadata block and the 10 px
# around it are what make the metadata read as one group. ``layout.md``:
# "Group related items to help people find the information they want."
_GAP_HERO_TITLE: int = 12
_GAPS = {
    "title": 0,
    "summary": 6,
    "origin": 10,
    "stats": 3,
    "tags": 10,
    "expiry": 10,
}

# Line clamps. The summary's is a line count and not a pixel height, so
# at 200 percent type it still shows five lines and the window grows.
# ``typography.md``: "Keep text truncation to a minimum as font size
# increases."
_TITLE_LINES: int = 3
_SUMMARY_LINES: int = 5
_ORIGIN_LINES: int = 2
_TAG_LINES: int = 2

# How much body text is worth keeping for the summary fallback. The line
# clamp cuts it long before this; the cap is here so a 60 KB article
# never goes through QTextLayout on hover.
_SUMMARY_SOURCE_CHARS: int = 400

_WORDS_PER_MINUTE: int = 200
_MAX_TAGS: int = 6

# NIP-37's default window is 90 days, so an expiry line on every draft
# would say nothing on almost every draft.
_EXPIRY_HORIZON_DAYS: int = 14

_NO_TITLE = "(no title)"
_SEP = " · "

_IMAGE_WAITING = "Loading image…"
_IMAGE_UNAVAILABLE = "Image unavailable"


# --------------------------------------------------------------------------- #
# Style                                                                       #
# --------------------------------------------------------------------------- #

def preview_css(is_dark: bool) -> str:
    """One QSS template for both themes, from the panel's token table.

    No ``font-size`` here, for the same reason the panel's sheet carries
    none: Qt treats both ``px`` and ``pt`` in a stylesheet as absolute
    and discards ``em``, so a size set here would freeze while the rest
    of the app scaled around it. QSS carries colour and weight only.

    Every pair is measured in both appearances. Title ``row_fg`` on
    ``chrome_bg`` is 15.31:1 dark and 16.39:1 light, summary
    ``chrome_fg`` 9.54 and 11.90, the metadata lines ``muted`` 4.74 and
    4.87, the expiry ``error_fg`` 6.24 and 4.66, and the image box label
    ``chrome_fg`` on ``border`` 6.87 and 9.66. ``accessibility.md``:
    "Text size / Text weight / Minimum contrast ratio / Up to 17 pts /
    All / 4.5:1", checked "in both light and dark appearances".
    """
    t = THEME_TOKENS[bool(is_dark)]
    return f"""
QLabel#drafts_preview_title {{ color: {t["row_fg"]}; font-weight: 600; }}
QLabel#drafts_preview_summary {{ color: {t["chrome_fg"]}; }}
QLabel#drafts_preview_origin,
QLabel#drafts_preview_stats,
QLabel#drafts_preview_tags {{ color: {t["muted"]}; }}
/* The word "Expires" carries the meaning and the colour only reinforces
   it. accessibility.md: "Convey information with more than color
   alone." */
QLabel#drafts_preview_expiry {{ color: {t["error_fg"]}; }}
QLabel#drafts_preview_image_note {{ color: {t["chrome_fg"]}; }}
"""


def title_font() -> QFont:
    """Title 3 from the macOS text-style table: 15 pt at a 13 pt base.

    A ratio of the application font rather than a literal, so the
    hierarchy survives a user who has enlarged type. ``typography.md``:
    "In general, avoid light font weights." Nothing here is lighter than
    Regular; hierarchy is carried by size, weight and the muted token.
    """
    font = QFont(QApplication.font())
    font.setPointSizeF(app_point_size() * 15.0 / BASE_POINT_SIZE)
    font.setWeight(QFont.DemiBold)
    return font


def body_font() -> QFont:
    """Body from the same table: the application font, unmodified."""
    return QFont(QApplication.font())


# --------------------------------------------------------------------------- #
# Record to strings                                                           #
# --------------------------------------------------------------------------- #

class BodyDigest(NamedTuple):
    """Everything derived from a draft's body, from one pass over it.

    Flattening a 60 KB article costs real time, and both the eligibility
    test and the composed fields need the result, so the pass happens
    once and the controller caches it per draft.
    """

    chars: int
    words: int
    minutes: int
    opening: str


class PreviewFields(NamedTuple):
    """What one preview shows, already composed, in reading order.

    Every field is a string and a missing one is empty, never a
    placeholder: the popover drops the line rather than printing "no
    summary". ``layout.md``: "Make essential information easy to find by
    giving it sufficient space. People want to view the most important
    information right away, so don't obscure it by crowding it with
    nonessential details."
    """

    title: str
    summary: str
    origin: str
    stats: str
    tags: str
    expiry: str
    image_url: str


def article_image_url(record: DraftRecord) -> str:
    """The hero image URL from the ``image`` tag, or '' when refused.

    The gate is deliberately tighter than either policy on its own, and
    it runs synchronously at open time so a refused URL is
    indistinguishable from an absent one and nothing is fetched.

    ``is_safe_media_url``, which ``ThumbnailLoader`` applies internally,
    permits plain http on loopback so a local Blossom dev server works.
    A hostile feed can stamp ``image`` as ``http://127.0.0.1:8080/x``,
    and resting a pointer on a row must never fire a request at the
    user's own machine. ``is_safe_mirror_source`` refuses userinfo and
    non-global IP literals but permits plain http, which is not wanted
    here either. The intersection is: https only, no userinfo, no
    private or link-local IP literals. ``ThumbnailLoader`` then
    re-applies its own policy, and its re-validation after redirects, on
    top of this.
    """
    url = first_tag(record, "image").strip()
    if not url:
        return ""
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return ""
    if scheme != "https":
        return ""
    if not url_safety.is_safe_mirror_source(url):
        return ""
    return url


def body_digest(content: str) -> BodyDigest:
    """Flatten a draft body once and keep everything derived from it.

    Words are counted from the flattened lines, so markdown syntax and
    image URLs never inflate the count. Reading time is 200 words per
    minute, floored at one minute.
    """
    lines = []
    for raw in str(content or "").split("\n"):
        line = flatten_markdown_inline(raw)
        if line:
            lines.append(line)
    flat = " ".join(lines)
    # A token has to carry a letter or a digit to count as a word.
    # ``flatten_markdown_inline`` deliberately leaves block markers
    # alone, because a preview snippet needs "## sub" and "#tag" as body
    # text, so without this a heading-heavy article counts one extra
    # word per heading and one per bullet.
    words = sum(
        1 for token in flat.split() if any(ch.isalnum() for ch in token)
    )
    minutes = max(1, math.ceil(words / _WORDS_PER_MINUTE)) if words else 0
    opening = flat
    if len(opening) > _SUMMARY_SOURCE_CHARS:
        cut = opening[:_SUMMARY_SOURCE_CHARS].rsplit(" ", 1)[0].rstrip()
        opening = f"{cut or opening[:_SUMMARY_SOURCE_CHARS]}…"
    return BodyDigest(len(flat), words, minutes, opening)


def reading_stats(content: str) -> Tuple[int, int]:
    """``(words, minutes)`` for a body, markdown syntax excluded.

    Both are shown, not just one. They answer different questions:
    reading time is "do I have time for this now", word count is "is
    this finished". For a writer looking at their own draft library the
    second is the one that matters, and it costs half a line.
    """
    digest = body_digest(content)
    return digest.words, digest.minutes


def article_hashtags(record: DraftRecord) -> List[str]:
    """Every ``t`` tag, deduplicated case-insensitively, first casing kept."""
    seen = set()
    out = []
    for tag in record.inner_tags or []:
        if not (isinstance(tag, list) and len(tag) >= 2 and tag[0] == "t"):
            continue
        value = str(tag[1]).strip().lstrip("#")
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def published_at(record: DraftRecord) -> int:
    """The NIP-23 ``published_at`` tag as a unix time, or 0."""
    try:
        return int(float(first_tag(record, "published_at")))
    except (TypeError, ValueError):
        return 0


def expiry_phrase(expiration: Optional[int], *, now: int) -> str:
    """"Expires in 3 days", or '' when the expiry is far off or absent.

    Disclosure, not the app's warning channel. ``popovers.md``: "Avoid
    using a popover to show a warning. People can miss a popover or
    accidentally close it." Nothing in the app may depend on the user
    having seen this line, and a proper expiry warning surface is a
    separate piece of work.
    """
    if not expiration:
        return ""
    delta = int(expiration) - int(now)
    if delta > _EXPIRY_HORIZON_DAYS * 86_400:
        return ""
    if delta < 0:
        return "Expired"
    days = delta // 86_400
    if days <= 0:
        return "Expires today"
    if days == 1:
        return "Expires in 1 day"
    return f"Expires in {days} days"


def _origin_line(record: DraftRecord) -> str:
    """Host, publication date and save time, joined in the panel's idiom.

    The host comes from ``source_host`` and the save time from
    ``format_absolute_time``, both reused verbatim, so the popover and
    the row cannot disagree about either.

    Where this exceeds the width it wraps at a ``·`` boundary and never
    elides. It is the only line whose parts are individually short
    enough that wrapping beats truncation.
    """
    parts = []
    host = source_host(record)
    if host:
        parts.append(host)
    published = published_at(record)
    # Two identical dates on one line is noise, so a draft stashed in the
    # same minute it was published shows only the save time.
    same_minute = (
        published > 0
        and record.created_at > 0
        and abs(published - record.created_at) < 60
    )
    if published > 0 and not same_minute:
        published_text = format_absolute_date(published)
        if published_text:
            parts.append(f"Published {published_text}")
    saved = format_absolute_time(record.created_at)
    if saved:
        parts.append(f"Saved {saved}")
    return _SEP.join(parts)


def _tags_line(names: List[str]) -> str:
    if not names:
        return ""
    shown = [f"#{name}" for name in names[:_MAX_TAGS]]
    extra = len(names) - len(shown)
    if extra > 0:
        shown.append(f"+{extra} more")
    return _SEP.join(shown)


def preview_fields(
    record: DraftRecord,
    *,
    now: int,
    digest: Optional[BodyDigest] = None,
) -> PreviewFields:
    """Compose one preview's strings. Pure, so the rules are unit tested.

    Reading order carries importance. ``layout.md``: "Place items to
    convey their relative importance." And ``typography.md``: "Adjust
    font weight, size, and color as needed to emphasize important
    information and help people visualize hierarchy."

    What is deliberately absent: the event id and the ``d`` tag, which
    are hex nobody reads and are a context-menu command away; the inner
    kind, which is 0 for every draft that has not decrypted so a badge
    would be absent or wrong exactly when it was needed; the failure
    reason, because a failed row shows no popover at all; the full
    source URL, because the host discriminates and the path does not;
    and the state, because the row carries it and the popover only opens
    for READY rows.

    The summary prefers the ``summary`` tag in full over the body, and
    is never ``record.snippet``: that is capped at 140 characters and is
    already on the row, and a popover that repeats its row verbatim is
    what ``popovers.md`` means by "too big".
    """
    if digest is None:
        digest = body_digest(record.content)
    summary = first_tag(record, "summary").strip() or digest.opening
    stats = (
        f"{digest.minutes} min read{_SEP}{digest.words:,} words"
        if digest.words else ""
    )
    return PreviewFields(
        title=record.title or _NO_TITLE,
        summary=summary,
        origin=_origin_line(record),
        stats=stats,
        tags=_tags_line(article_hashtags(record)),
        expiry=expiry_phrase(record.expiration, now=now),
        image_url=article_image_url(record),
    )


def preview_is_eligible(
    record: DraftRecord,
    *,
    now: int,
    digest: Optional[BodyDigest] = None,
) -> bool:
    """Whether a preview would show anything the row does not already.

    A 360 px window that repeats a row verbatim is exactly what
    ``popovers.md`` means by "Avoid making a popover too big". In
    practice every imported article qualifies and a two-word note does
    not.

    LOADING and FAILED never qualify. The only fact a loading row has is
    the word "Decrypting", which the row already says twice; and for a
    failure ``popovers.md`` is explicit, "Avoid using a popover to show
    a warning... If you need to warn people, use an alert instead", so
    the failure reason and the retry hint stay in the row's own text
    where they cannot be missed or accidentally dismissed.
    """
    if record.state is not DraftState.READY:
        return False
    if article_image_url(record):
        return True
    if first_tag(record, "summary").strip():
        return True
    if article_hashtags(record):
        return True
    if published_at(record) > 0:
        return True
    if expiry_phrase(record.expiration, now=now):
        return True
    # Compared flattened, because the snippet is flattened too: a note
    # whose row already shows its whole body must not open a window to
    # show the same words again.
    if digest is None:
        digest = body_digest(record.content)
    return digest.chars > len(record.snippet or "")


def preview_announcement(
    record: DraftRecord,
    *,
    now: int,
    digest: Optional[BodyDigest] = None,
) -> str:
    """The extra sentences a screen reader hears on the row itself.

    Route 3 of three, and the one that depends on neither a pointer nor
    a popover. It is what makes the preview a visual convenience rather
    than an information channel, which is the only way to satisfy
    ``accessibility.md``'s "Prefer dismissing views with an explicit
    action" for a surface that dismisses itself on a timer.

    "6 minute read" and not "6 min read", "1240" and not "1,240":
    abbreviations and thousands separators are read aloud badly.
    ``inclusion.md``: "Avoid using specialized or technical terms
    without defining them." The visible line keeps its compact form,
    only the spoken one expands.

    The hero image contributes nothing. NIP-23 carries no alt text for
    the ``image`` tag, so there is nothing truthful to say about it, and
    inventing a description would be worse than silence.
    """
    if record.state is not DraftState.READY:
        return ""
    if digest is None:
        digest = body_digest(record.content)
    sentences = []
    if digest.words:
        minutes = "minute" if digest.minutes == 1 else "minutes"
        words = "word" if digest.words == 1 else "words"
        sentences.append(
            f"{digest.minutes} {minutes} read, {digest.words} {words}"
        )
    names = article_hashtags(record)
    if names:
        sentences.append("Tagged " + ", ".join(names[:_MAX_TAGS]))
    expiry = expiry_phrase(record.expiration, now=now)
    if expiry:
        sentences.append(expiry)
    return ". ".join(sentences)


# --------------------------------------------------------------------------- #
# Placement                                                                   #
# --------------------------------------------------------------------------- #

class Placement(NamedTuple):
    """Where the popover's visible surface goes, and where its arrow is.

    ``rect`` is the surface in global coordinates, never the top-level
    window: the window is larger by the transparent halo that holds the
    shadow and the arrow, and that halo is allowed to hang off the edge
    of the screen because nothing is painted in it there.

    ``arrow`` is the tip's offset along the edge the arrow sits on, in
    surface-local coordinates, or -1 when the clamp has moved the
    surface far enough that no honest arrow can be drawn.
    """

    rect: QRect
    side: str        # "left" | "right" | "below" | "above"
    arrow: int


def _clamp_axis(value: int, size: int, start: int, end: int, margin: int) -> int:
    """Place a ``size``-long box inside ``[start, end)``, near ``value``."""
    low = start + margin
    high = end - margin - size
    if high < low:
        # A screen shorter than the margins allow for. Centre the box
        # rather than letting it hang off one edge.
        return start + max(0, (end - start - size) // 2)
    return max(low, min(value, high))


def _arrow_offset(tip: int, extent: int) -> int:
    """The arrow's position along an edge, or -1 when it cannot be drawn."""
    if tip < _ARROW_INSET or tip > extent - _ARROW_INSET:
        return -1
    return tip


def preview_geometry(
    anchor: QRect,
    size: QSize,
    available: QRect,
    *,
    gap: int = PREVIEW_GAP_PX,
) -> Optional[Placement]:
    """Place a ``size`` surface beside ``anchor``, inside ``available``.

    Order of preference, first that fits wins: left of the row, right of
    the row, below it, above it. Left is preferred because the panel is
    docked at the right edge of the window, so the right side is off
    screen by definition and the leading side points back into the
    editor.

    Below and above are last resorts because on a right-docked panel
    they put the window over the list and hide neighbouring rows.
    ``popovers.md``: "Ideally, a popover doesn't cover the element that
    revealed it or any essential content people may need to see while
    using it", and the neighbouring rows are exactly what a person
    comparing drafts needs to see.

    Returns ``None`` when nothing fits. A preview that would cover the
    row it describes is not shown at all: the promise above is not
    negotiable, and a covered row means the user cannot see what the
    preview is about.

    ``available`` is a screen's *available* geometry, never its full
    geometry, so the popover never lands under the menu bar, the Dock or
    a Windows taskbar. ``layout.md``, macOS: "Avoid placing controls or
    critical information at the bottom of a window. People often move
    windows so that the bottom edge is below the bottom of the screen."
    """
    width, height = size.width(), size.height()
    if width <= 0 or height <= 0:
        return None
    if width > available.width() or height > available.height():
        return None

    av_left, av_top = available.left(), available.top()
    av_right, av_bottom = available.right() + 1, available.bottom() + 1
    a_left, a_top = anchor.left(), anchor.top()
    a_right, a_bottom = anchor.right() + 1, anchor.bottom() + 1

    def beside(x: int) -> Placement:
        y = _clamp_axis(
            anchor.center().y() - height // 2, height,
            av_top, av_bottom, _SCREEN_MARGIN,
        )
        side = "left" if x < a_left else "right"
        return Placement(
            QRect(x, y, width, height), side,
            _arrow_offset(anchor.center().y() - y, height),
        )

    def stacked(y: int, side: str) -> Placement:
        x = _clamp_axis(
            anchor.center().x() - width // 2, width,
            av_left, av_right, _SCREEN_MARGIN,
        )
        return Placement(
            QRect(x, y, width, height), side,
            _arrow_offset(anchor.center().x() - x, width),
        )

    if a_left - gap - width >= av_left:
        return beside(a_left - gap - width)
    if a_right + gap + width <= av_right:
        return beside(a_right + gap)
    if a_bottom + gap + height <= av_bottom:
        return stacked(a_bottom + gap, "below")
    if a_top - gap - height >= av_top:
        return stacked(a_top - gap - height, "above")
    return None


def preview_width(available: QRect) -> int:
    """Content width, scaled with the application font and clamped.

    Clamped for the same reason ``secondary_font`` is: unbounded growth
    at 200 percent type produces a window nobody can place.
    """
    grown = round(_BASE_WIDTH * app_point_size() / BASE_POINT_SIZE)
    width = max(_MIN_WIDTH, min(grown, _MAX_WIDTH))
    return max(120, min(width, available.width() - 48))


def preview_max_height(available: QRect) -> int:
    """Tallest the surface may compose to before the degrade ladder runs."""
    grown = round(_BASE_MAX_HEIGHT * app_point_size() / BASE_POINT_SIZE)
    return max(120, min(grown, available.height() - 32))


def uses_child_window(platform_name: str) -> bool:
    """Whether the popover must be a child widget rather than a window.

    Wayland does not let a frameless ``Qt.Tool`` top level position
    itself at absolute global coordinates, so there the popover is built
    as a child of the panel's window and placed in window coordinates.
    It then cannot extend past the window edge, which on a right-docked
    panel is acceptable because the preferred side points inward.
    """
    return str(platform_name or "").lower().startswith("wayland")


def screen_available_rect(anchor: QRect, fallback: Optional[QWidget]) -> QRect:
    """The available rect of the screen the anchor is on.

    Resolved from the anchor point rather than from the window or the
    primary screen, so a panel on a 4K monitor to the left of a laptop
    screen flips correctly at the seam. ``screenAt`` returns ``None``
    for a point outside every screen, which happens for a second or two
    after a monitor is unplugged while the window is still on the old
    coordinates; the two fallbacks make that a non-event.
    """
    screen = QGuiApplication.screenAt(anchor.center())
    if screen is None and fallback is not None:
        window = fallback.window()
        screen = window.screen() if window is not None else None
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        return QRect(0, 0, 1024, 768)
    return screen.availableGeometry()


# --------------------------------------------------------------------------- #
# Labels                                                                      #
# --------------------------------------------------------------------------- #

class _ClampedLabel(QLabel):
    """Word-wrapped label capped at N lines, eliding the last one.

    Not a ``QLabel`` with ``wordWrap`` and a fixed height: that hard
    clips the last line with no sign anything was cut, which is exactly
    the bug ``_ElidingLabel`` exists to prevent and which produced a row
    reading ``![One Class, One`` with no trailing dots.

    Laid out at paint time from ``contentsRect().width()`` for the same
    reason ``_ElidingLabel`` elides there: eliding through ``setText``
    changes the size hint, which re-runs the layout, which moves the
    width the elision was computed against.

    The height is fixed by :meth:`apply` from the width the popover was
    composed at, so the surface can be measured before it is shown and
    never resizes underneath a reader.
    """

    def __init__(self, object_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self._full = ""
        self._max_lines = 1
        self._width = 1
        self.setTextInteractionFlags(Qt.NoTextInteraction)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def full_text(self) -> str:
        return self._full

    def max_lines(self) -> int:
        return self._max_lines

    def apply(self, text: str, *, width: int, max_lines: int) -> int:
        """Set the text and fix the height. Returns the height taken."""
        self._full = text or ""
        self._max_lines = max(1, int(max_lines))
        self._width = max(1, int(width))
        # The painted string may be elided and ``text()`` stays empty, so
        # the accessible name is where the whole string stays reachable.
        self.setAccessibleName(self._full)
        height = len(self._ranges(self._width)[0]) * self.fontMetrics().lineSpacing()
        self.setFixedHeight(height)
        self.setVisible(bool(self._full))
        self.update()
        return height

    def line_count(self) -> int:
        return len(self._ranges(self._width)[0])

    def is_truncated(self) -> bool:
        return self._ranges(self._width)[1]

    def painted_lines(self) -> List[str]:
        """Exactly the strings ``paintEvent`` will draw, ellipsis and all."""
        width = max(1, self.contentsRect().width() or self._width)
        ranges, truncated = self._ranges(width)
        out = []
        for index, (start, length) in enumerate(ranges):
            if truncated and index == len(ranges) - 1:
                out.append(self.fontMetrics().elidedText(
                    self._full[start:], Qt.ElideRight, width,
                ))
            else:
                out.append(self._full[start:start + length])
        return out

    def _ranges(self, width: int) -> Tuple[List[Tuple[int, int]], bool]:
        if not self._full:
            return [], False
        layout = QTextLayout(self._full, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        ranges: List[Tuple[int, int]] = []
        truncated = False
        while len(ranges) < self._max_lines:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            ranges.append((line.textStart(), line.textLength()))
        else:
            truncated = layout.createLine().isValid()
        layout.endLayout()
        return ranges, truncated

    def paintEvent(self, event) -> None:
        if not self._full:
            return
        painter = QPainter(self)
        painter.setFont(self.font())
        # Through the palette, which is where the stylesheet's ``color``
        # lands after a polish, so the token table stays the only source
        # of colour here as well.
        painter.setPen(QPen(self.palette().color(self.foregroundRole())))
        rect = self.contentsRect()
        metrics = self.fontMetrics()
        y = rect.top() + metrics.ascent()
        for text in self.painted_lines():
            painter.drawText(rect.left(), y, text)
            y += metrics.lineSpacing()


class _HeroImage(QWidget):
    """The 16:9 hero box, whose geometry never depends on the bytes.

    A fixed box rather than the image's own ratio, so frames one, two
    and three are identical in size: reserved and empty, reserved and
    labelled, and filled. Nothing below it ever moves, and the window is
    never resized after it is shown.

    ``layout.md``: "When necessary, scale artwork in response to display
    changes... don't change the aspect ratio of the artwork; instead,
    scale it so that important visual content remains visible."
    Expanding plus a centre crop preserves the ratio and keeps the
    middle, which is where a hero image puts its subject.

    No spinner, no shimmer, no skeleton animation. Qt exposes no
    portable reduce-motion query, so the only safe design is one that
    needs no toggle: a static fill and a word.
    """

    def __init__(self, parent: Optional[QWidget] = None, *, is_dark: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_preview_image")
        self._is_dark = bool(is_dark)
        self._pixmap: Optional[QPixmap] = None
        # NIP-23 carries no alt text, so the name says what the thing is
        # and no description invents what it depicts.
        self.setAccessibleName("Article image")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(_PAD, _PAD, _PAD, _PAD)
        self._note = QLabel("", self)
        self._note.setObjectName("drafts_preview_image_note")
        self._note.setAlignment(Qt.AlignCenter)
        self._note.setWordWrap(True)
        self._note.hide()
        layout.addWidget(self._note)

    def note(self) -> QLabel:
        return self._note

    def note_text(self) -> str:
        return self._note.text() if self._note.isVisibleTo(self) else ""

    def has_pixmap(self) -> bool:
        return self._pixmap is not None and not self._pixmap.isNull()

    def reset(self, *, width: int) -> None:
        self._pixmap = None
        self._note.setText("")
        self._note.setFont(secondary_font())
        self._note.hide()
        pad = scaled(_PAD)
        self.layout().setContentsMargins(pad, pad, pad, pad)
        self.setFixedSize(width, round(width * 9 / 16))
        self.update()

    def set_note(self, text: str) -> None:
        """Say what is happening in the reserved box, without resizing it."""
        self._note.setText(text)
        self._note.setVisible(bool(text) and not self.has_pixmap())
        self.update()

    def set_pixmap(self, pixmap: QPixmap) -> None:
        if pixmap is None or pixmap.isNull():
            return
        box = self.size()
        # ``images.md``: "High-resolution 2D displays have higher pixel
        # densities, such as 2:1 or 3:1." A hero rendered at 1x on a 2x
        # display is visibly soft next to the crisp text beside it.
        dpr = float(self.devicePixelRatioF() or 1.0)
        target = QSize(
            max(1, round(box.width() * dpr)), max(1, round(box.height() * dpr)),
        )
        grown = pixmap.scaled(
            target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
        )
        x = max(0, (grown.width() - target.width()) // 2)
        y = max(0, (grown.height() - target.height()) // 2)
        cropped = grown.copy(x, y, target.width(), target.height())
        cropped.setDevicePixelRatio(dpr)
        self._pixmap = cropped
        self._note.hide()
        self.update()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect()
        # Top corners follow the surface; the bottom ones are square
        # because the text block continues straight below.
        path = QPainterPath()
        path.moveTo(rect.left(), rect.bottom() + 1)
        path.lineTo(rect.left(), rect.top() + _RADIUS)
        path.quadTo(rect.left(), rect.top(), rect.left() + _RADIUS, rect.top())
        path.lineTo(rect.right() + 1 - _RADIUS, rect.top())
        path.quadTo(
            rect.right() + 1, rect.top(), rect.right() + 1, rect.top() + _RADIUS,
        )
        path.lineTo(rect.right() + 1, rect.bottom() + 1)
        path.closeSubpath()
        painter.setClipPath(path)
        # The fill is deliberately quiet: ``border`` on ``chrome_bg`` is
        # 1.39:1 dark and 1.23:1 light, and a placeholder fill is not
        # text. The label on top of it is what carries meaning, at
        # 6.87:1 and 9.66:1.
        painter.fillRect(rect, QColor(THEME_TOKENS[self._is_dark]["border"]))
        if self.has_pixmap():
            painter.drawPixmap(rect, self._pixmap)


# --------------------------------------------------------------------------- #
# The popover                                                                 #
# --------------------------------------------------------------------------- #

class Plan(NamedTuple):
    """One rung of the degrade ladder."""

    summary_lines: int
    tags: bool
    image: bool


# Degrade in this order and stop as soon as it fits. Hero last, because
# it is the reason the user hovered. Summary first, because the first two
# lines of a summary carry most of its value. The title and the origin
# line are never dropped, which honours ``typography.md``: "Maintain a
# consistent information hierarchy regardless of the current font size."
LADDER: Tuple[Plan, ...] = (
    Plan(_SUMMARY_LINES, True, True),
    Plan(4, True, True),
    Plan(3, True, True),
    Plan(2, True, True),
    Plan(2, False, True),
    Plan(2, False, False),
)


class _Surface(QWidget):
    """The visible card plus the arrow, painted from the token table.

    Arrow room is reserved on all four sides rather than only on the
    pointing one, so the window's size does not change when the flip
    rule picks a different side. The reservation is transparent, so it
    costs nothing but pixels nobody paints.
    """

    def __init__(self, parent: Optional[QWidget] = None, *, is_dark: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("drafts_preview_surface")
        self._is_dark = bool(is_dark)
        self._side = "left"
        self._arrow = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            _ARROW_DEPTH, _ARROW_DEPTH, _ARROW_DEPTH, _ARROW_DEPTH,
        )
        self._card = QWidget(self)
        layout.addWidget(self._card)

    def card(self) -> QWidget:
        return self._card

    def side(self) -> str:
        return self._side

    def arrow(self) -> int:
        return self._arrow

    def set_card_size(self, size: QSize) -> None:
        self._card.setFixedSize(size)
        self.setFixedSize(
            size.width() + 2 * _ARROW_DEPTH, size.height() + 2 * _ARROW_DEPTH,
        )

    def set_arrow(self, side: str, offset: int) -> None:
        self._side = side
        self._arrow = int(offset)
        self.update()

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self.update()

    def paintEvent(self, event) -> None:
        tokens = THEME_TOKENS[self._is_dark]
        fill = QColor(tokens["chrome_bg"])
        border = QColor(tokens["border"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        card = self._card.geometry()
        painter.setPen(QPen(border, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(card.adjusted(0, 0, -1, -1), _RADIUS, _RADIUS)
        if self._arrow < 0:
            return
        tip, base_a, base_b = self._arrow_points(card)
        if tip is None:
            return
        path = QPainterPath()
        path.moveTo(base_a)
        path.lineTo(tip)
        path.lineTo(base_b)
        painter.setPen(Qt.NoPen)
        painter.setBrush(fill)
        painter.drawPath(path)
        # The two outer edges only, so the arrow reads as part of the
        # surface rather than as a separate glyph beside it.
        painter.setPen(QPen(border, 1))
        painter.drawLine(base_a, tip)
        painter.drawLine(tip, base_b)

    def _arrow_points(self, card: QRect):
        half = _ARROW_BASE // 2
        offset = self._arrow
        if self._side == "left":
            y = card.top() + offset
            return (
                QPoint(card.right() + _ARROW_DEPTH, y),
                QPoint(card.right(), y - half),
                QPoint(card.right(), y + half),
            )
        if self._side == "right":
            y = card.top() + offset
            return (
                QPoint(card.left() - _ARROW_DEPTH, y),
                QPoint(card.left(), y - half),
                QPoint(card.left(), y + half),
            )
        if self._side == "below":
            x = card.left() + offset
            return (
                QPoint(x, card.top() - _ARROW_DEPTH),
                QPoint(x - half, card.top()),
                QPoint(x + half, card.top()),
            )
        if self._side == "above":
            x = card.left() + offset
            return (
                QPoint(x, card.bottom() + _ARROW_DEPTH),
                QPoint(x - half, card.bottom()),
                QPoint(x + half, card.bottom()),
            )
        return None, None, None


class PreviewPopover(QWidget):
    """One draft, rendered as a floating surface.

    Renderable from a record alone: :meth:`compose` needs no panel, no
    store and no screen, so the whole composition including the degrade
    ladder is testable and screenshottable on its own.

    Opaque, not translucent. ``materials.md``: "Thicker materials, which
    are more opaque, can provide better contrast for text and other
    elements with fine features", and fully opaque is the far end of
    that. Qt gives no Liquid Glass, and the panel's cross-platform
    discipline forbids a per-platform blur path.

    It carries no interactive content at all. ``popovers.md`` asks to
    "limit the amount of functionality in the popover to a few related
    tasks"; here the number of tasks is zero, and everything actionable
    stays in the context menu, which is reachable by pointer and by the
    Menu key.
    """

    dismissed = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        is_dark: bool = True,
        child_mode: bool = False,
    ) -> None:
        super().__init__(
            parent, Qt.Widget if child_mode else (Qt.Tool | Qt.FramelessWindowHint),
        )
        self._is_dark = bool(is_dark)
        self._child_mode = bool(child_mode)
        self._card = QSize(0, 0)
        self._fields = PreviewFields("", "", "", "", "", "", "")
        self._plan = LADDER[0]
        self._closing = False
        # With no loader wired up there is no way to fill a hero box, so
        # none is reserved: a permanently empty 203 px rectangle is worse
        # than a shorter popover.
        self._images_enabled = False

        self.setObjectName("drafts_preview")
        self.setAccessibleName("Draft preview")
        # Qt has no per-widget accessible-role setter, so this window
        # cannot advertise itself as a dialog the way an AppKit popover
        # does. Every child below carries its own accessible name
        # instead, so a reader that does enter the window moves through
        # title, summary and metadata rather than hearing one paragraph,
        # and ``preview_announcement`` puts the same facts on the row for
        # a reader that never enters it at all.
        self.setAccessibleDescription(
            "Preview of one draft. Press Escape to close."
        )
        self.setFocusPolicy(Qt.NoFocus)
        if not self._child_mode:
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)

        outer = QVBoxLayout(self)
        margin = self._shadow_margin()
        outer.setContentsMargins(margin, margin, margin, margin)
        self._surface = _Surface(self, is_dark=self._is_dark)
        outer.addWidget(self._surface)

        card = self._surface.card()
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        self._hero = _HeroImage(card, is_dark=self._is_dark)
        card_layout.addWidget(self._hero)
        self._body = QWidget(card)
        card_layout.addWidget(self._body)
        card_layout.addStretch(1)

        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(_PAD, _PAD, _PAD, _PAD)
        body_layout.setSpacing(0)
        self._title = _ClampedLabel("drafts_preview_title", self._body)
        self._summary = _ClampedLabel("drafts_preview_summary", self._body)
        self._origin = _ClampedLabel("drafts_preview_origin", self._body)
        self._stats = _ClampedLabel("drafts_preview_stats", self._body)
        self._tags = _ClampedLabel("drafts_preview_tags", self._body)
        self._expiry = _ClampedLabel("drafts_preview_expiry", self._body)
        self._rows = (
            ("title", self._title),
            ("summary", self._summary),
            ("origin", self._origin),
            ("stats", self._stats),
            ("tags", self._tags),
            ("expiry", self._expiry),
        )
        self._spacers = {}
        for name, label in self._rows:
            spacer = QWidget(self._body)
            spacer.setFixedHeight(scaled(_GAPS[name]) if _GAPS[name] else 0)
            spacer.hide()
            body_layout.addWidget(spacer)
            body_layout.addWidget(label)
            self._spacers[name] = spacer

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.finished.connect(self._on_fade_finished)
        self._move = QPropertyAnimation(self, b"geometry", self)
        # Nothing inside is interactive, so nothing inside needs the
        # pointer. Handing every child's mouse events to the window is
        # what lets the controller see one Enter and one Leave for the
        # whole surface instead of a stream of them from whichever label
        # the pointer happens to be over.
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # One effect for the widget's lifetime, recoloured on a theme
        # switch. ``setGraphicsEffect`` destroys whatever was there
        # before, so building a fresh one per theme change would leave a
        # trail of dead objects behind their Python wrappers.
        self._shadow = QGraphicsDropShadowEffect(self._surface)
        self._shadow.setBlurRadius(_SHADOW_BLUR)
        self._shadow.setXOffset(0)
        self._shadow.setYOffset(_SHADOW_DY)
        self._surface.setGraphicsEffect(self._shadow)
        self.apply_theme(self._is_dark)

    # -- geometry of the frame --------------------------------------------

    def is_child_window(self) -> bool:
        """True on Wayland, where a frameless tool window cannot place itself."""
        return self._child_mode

    def _shadow_margin(self) -> int:
        return 0 if self._child_mode else _SHADOW_MARGIN

    def halo(self) -> int:
        """Transparent border around the card: shadow plus arrow room."""
        return self._shadow_margin() + _ARROW_DEPTH

    def card_size(self) -> QSize:
        return QSize(self._card)

    # -- theme -------------------------------------------------------------

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        self.setStyleSheet(preview_css(self._is_dark))
        self._surface.apply_theme(self._is_dark)
        self._hero.apply_theme(self._is_dark)
        # The shadow is what separates the surface from arbitrary editor
        # content behind it, which no border token can do on its own, so
        # it is heavier in dark where the border has least to work with.
        self._shadow.setColor(QColor(0, 0, 0, 115 if self._is_dark else 56))
        for _name, label in self._rows:
            label.style().unpolish(label)
            label.style().polish(label)
        note = self._hero.note()
        note.style().unpolish(note)
        note.style().polish(note)
        self.update()

    # -- composition -------------------------------------------------------

    def fields(self) -> PreviewFields:
        return self._fields

    def plan(self) -> Plan:
        return self._plan

    def labels(self) -> Tuple[Tuple[str, _ClampedLabel], ...]:
        return self._rows

    def set_images_enabled(self, enabled: bool) -> None:
        """Whether a hero box may be reserved at all. Set before composing."""
        self._images_enabled = bool(enabled)

    def reserved_image(self) -> bool:
        """Whether a hero box exists. Decided once, at open, never revisited."""
        return not self._hero.isHidden()

    def compose(
        self,
        record: DraftRecord,
        *,
        width: int,
        max_height: int,
        now: int,
        digest: Optional[BodyDigest] = None,
    ) -> Optional[QSize]:
        """Lay the record out at ``width``, degrading until it fits.

        The ladder runs once, here, at open time. Nothing in it may run
        later, or the window would resize while it was being read.
        Returns the card size, or ``None`` when even the last rung is
        taller than ``max_height``.
        """
        self._fields = preview_fields(record, now=now, digest=digest)
        self._title.setFont(title_font())
        self._summary.setFont(body_font())
        footnote = secondary_font()
        for label in (self._origin, self._stats, self._tags, self._expiry):
            label.setFont(footnote)

        for plan in LADDER:
            height = self._apply_plan(plan, width=width)
            if height <= max_height:
                self._plan = plan
                self._card = QSize(width, height)
                self._surface.set_card_size(self._card)
                halo = self.halo()
                self.resize(width + 2 * halo, height + 2 * halo)
                return QSize(self._card)
        return None

    def _apply_plan(self, plan: Plan, *, width: int) -> int:
        fields = self._fields
        show_image = plan.image and self._images_enabled and bool(fields.image_url)
        self._hero.setVisible(show_image)
        if show_image:
            self._hero.reset(width=width)
        # One padding value for the margins and for the width the lines
        # are measured against. Measuring against a different width from
        # the one the labels are given is how a clamp ends up drawing a
        # line more than it counted, and clipping it.
        pad = scaled(_PAD)
        inner = max(1, width - 2 * pad)

        heights = {
            "title": self._title.apply(
                fields.title, width=inner, max_lines=_TITLE_LINES),
            "summary": self._summary.apply(
                fields.summary, width=inner, max_lines=plan.summary_lines),
            "origin": self._origin.apply(
                fields.origin, width=inner, max_lines=_ORIGIN_LINES),
            "stats": self._stats.apply(
                fields.stats, width=inner, max_lines=1),
            "tags": self._tags.apply(
                fields.tags if plan.tags else "", width=inner, max_lines=_TAG_LINES),
            "expiry": self._expiry.apply(
                fields.expiry, width=inner, max_lines=1),
        }
        # A spacer only earns its place between two lines that are both
        # there, so a draft with no summary does not carry the gap that
        # would have sat under one.
        first = True
        for name, _label in self._rows:
            spacer = self._spacers[name]
            # Re-derived here rather than at construction, so the rhythm
            # follows the application font rather than whatever size it
            # happened to be when the window was first built.
            spacer.setFixedHeight(scaled(_GAPS[name]) if _GAPS[name] else 0)
            spacer.setVisible(bool(heights[name]) and not first)
            if heights[name]:
                first = False
        # The hero replaces the body's top padding with the tighter gap
        # the rhythm asks for, so the image sits closer to its title than
        # to the popover's edge.
        top = scaled(_GAP_HERO_TITLE) if show_image else pad
        body = self._body.layout()
        body.setContentsMargins(pad, top, pad, pad)
        body.invalidate()
        height = self._body.sizeHint().height()
        if show_image:
            height += self._hero.height()
        return height

    # -- placement ---------------------------------------------------------

    def place(self, placement: Placement, *, animate: bool = False) -> None:
        """Move the window so its card lands on ``placement.rect``."""
        self._surface.set_arrow(placement.side, placement.arrow)
        halo = self.halo()
        rect = placement.rect
        top_left = QPoint(rect.left() - halo, rect.top() - halo)
        if self._child_mode:
            parent = self.parentWidget()
            if parent is not None:
                top_left = parent.mapFromGlobal(top_left)
        target = QRect(
            top_left,
            QSize(rect.width() + 2 * halo, rect.height() + 2 * halo),
        )
        self._move.stop()
        if animate and PREVIEW_SWAP_DELAY_MS > 0 and self.isVisible():
            # ``popovers.md``: "If you adjust the size of a popover,
            # animate the change to avoid giving the impression that a
            # new popover replaced the old one."
            self._move.setDuration(PREVIEW_SWAP_DELAY_MS)
            self._move.setStartValue(self.geometry())
            self._move.setEndValue(target)
            self._move.start()
        else:
            self.setGeometry(target)

    def side(self) -> str:
        return self._surface.side()

    def arrow_offset(self) -> int:
        return self._surface.arrow()

    def card_rect(self) -> QRect:
        """The visible surface in global coordinates.

        Not ``geometry()``: the window is larger by the transparent halo
        that carries the shadow and the arrow, and it is the card, not
        the halo, that has to clear the row it describes.
        """
        halo = self.halo()
        return QRect(self.mapToGlobal(QPoint(halo, halo)), self._card)

    # -- image -------------------------------------------------------------

    def set_image_pixmap(self, pixmap: QPixmap) -> None:
        if self.reserved_image():
            self._hero.set_pixmap(pixmap)

    def set_image_note(self, text: str) -> None:
        if self.reserved_image():
            self._hero.set_note(text)

    def image_note(self) -> str:
        return self._hero.note_text()

    def has_image(self) -> bool:
        return self._hero.has_pixmap()

    # -- show and hide -----------------------------------------------------

    def _fade_ms(self) -> int:
        return 0 if self._child_mode else max(0, PREVIEW_FADE_MS)

    def show_preview(self, *, keyboard: bool = False) -> None:
        """Show without stealing focus, unless the keyboard opened it.

        The named requirement is that a preview must never take focus
        while the user is typing in the search field, and the two flags
        below make that structurally impossible rather than
        conditionally avoided. A keyboard opening drops both, because
        without focus the screen reader cursor never enters the window
        and the keyboard route would be theatre.
        """
        self._closing = False
        self._fade.stop()
        if not self._child_mode:
            self.setWindowFlag(Qt.WindowDoesNotAcceptFocus, not keyboard)
            self.setAttribute(Qt.WA_ShowWithoutActivating, not keyboard)
        focus = Qt.StrongFocus if keyboard else Qt.NoFocus
        self.setFocusPolicy(focus)
        self._title.setFocusPolicy(focus)
        fade = self._fade_ms()
        self.setWindowOpacity(0.0 if fade else 1.0)
        self.show()
        self.raise_()
        if keyboard:
            self._title.setFocus(Qt.OtherFocusReason)
        if fade:
            self._fade.setDuration(fade)
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()

    def close_preview(self) -> None:
        self._move.stop()
        if not self.isVisible():
            return
        fade = self._fade_ms()
        if not fade:
            self._finish_close()
            return
        self._closing = True
        self._fade.stop()
        self._fade.setDuration(fade)
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_fade_finished(self) -> None:
        if self._closing:
            self._finish_close()

    def _finish_close(self) -> None:
        self._closing = False
        self._fade.stop()
        self.hide()
        self.setWindowOpacity(1.0)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            event.accept()
            self.dismissed.emit()
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------------------- #
# Controller                                                                  #
# --------------------------------------------------------------------------- #

class PreviewController(QObject):
    """Owns the dwell timing, the one popover, and every dismissal path.

    ``popovers.md``: "Show one popover at a time. Displaying multiple
    popovers clutters the interface and causes confusion." There is
    exactly one :class:`PreviewPopover` for the panel's lifetime, hidden
    rather than destroyed between shows.

    The controller never holds a widget pointer for the shown draft. It
    holds the identifier and an anchor ``QRect`` in global coordinates,
    because ``_rebuild_list`` destroys and recreates every row widget on
    a search keystroke and a held ``_DraftRowWidget`` would dangle
    exactly as ``_on_current_item_changed`` already had to guard against
    with its ``try / except RuntimeError``.

    The panel is the only thing it talks to, through four small methods:
    ``preview_record``, ``preview_current_row``, ``preview_is_available``
    and ``preview_now``. Everything else is injected or pure.
    """

    def __init__(self, panel, *, parent: Optional[QObject] = None) -> None:
        super().__init__(parent if parent is not None else panel)
        self._panel = panel
        self._popover: Optional[PreviewPopover] = None
        self._loader = None
        self._is_dark = True

        self._pending_id = ""
        self._pending_anchor = QRect()
        self._shown_id = ""
        self._shown_anchor = QRect()
        # The row the pointer is inside right now, which is not the same
        # as the row a preview is pending or shown for. It exists so a
        # draft that finishes decrypting under a resting pointer can arm
        # without the user moving off the row and back.
        self._hovered_id = ""
        self._hovered_anchor = QRect()
        self._keyboard_open = False
        self._disarmed = False
        self._image_url = ""
        # Per session, so a broken hero is not re-requested every time
        # the pointer crosses that row.
        self._image_failures: Dict[str, str] = {}
        # Keyed by identifier and body length, so sweeping a list of long
        # articles does not re-tokenise 60 KB per row.
        self._digests: Dict[Tuple[str, int], BodyDigest] = {}
        self._watched_window: Optional[QWidget] = None

        # Every timer is parented to this controller, which is a child of
        # the panel, so panel destruction cannot leave one firing into a
        # dead reference.
        self._dwell = QTimer(self)
        self._dwell.setSingleShot(True)
        self._dwell.timeout.connect(self._on_dwell)
        self._closer = QTimer(self)
        self._closer.setSingleShot(True)
        self._closer.timeout.connect(self.close)
        self._slow = QTimer(self)
        self._slow.setSingleShot(True)
        self._slow.timeout.connect(self._on_image_slow)

    # -- wiring ------------------------------------------------------------

    def set_image_loader(self, loader) -> None:
        """Inject the shared ``ThumbnailLoader``, or ``None`` for no images.

        The default is ``None``, and with ``None`` the popover renders
        with no hero image at all, ever. A panel built in a test
        therefore cannot touch a network, a relay, a signer or the real
        ``~/.config`` unless the test deliberately injects a loader.
        """
        if self._loader is loader:
            return
        self._disconnect_loader()
        self._loader = loader
        if loader is not None:
            loader.url_ready.connect(self._on_image_ready)
            loader.url_failed.connect(self._on_image_failed)

    def _disconnect_loader(self) -> None:
        if self._loader is None:
            return
        try:
            self._loader.url_ready.disconnect(self._on_image_ready)
            self._loader.url_failed.disconnect(self._on_image_failed)
        except (RuntimeError, TypeError):
            pass
        self._loader = None

    def attach_row(self, row: QWidget) -> None:
        """Watch one row for hover. Called from the panel's ``_insert_row``.

        The filter cannot go on the list's viewport: the row widget
        covers the item rect exactly and is not mouse transparent, so
        the viewport never sees the pointer.
        """
        row.setMouseTracking(True)
        row.installEventFilter(self)

    def attach_list(self, list_widget) -> None:
        """Watch the list for the Space key and for any scrolling."""
        list_widget.installEventFilter(self)
        bar = list_widget.verticalScrollBar()
        if bar is not None:
            bar.valueChanged.connect(self._on_scrolled)

    def apply_theme(self, is_dark: bool) -> None:
        self._is_dark = bool(is_dark)
        if self._popover is not None:
            # Re-style in place. A theme switch is not a reason to take
            # the surface away from someone reading it.
            self._popover.apply_theme(self._is_dark)

    # -- state -------------------------------------------------------------

    def is_open(self) -> bool:
        return bool(self._shown_id) and self.is_visible()

    def is_visible(self) -> bool:
        return self._popover is not None and self._popover.isVisible()

    def shown_identifier(self) -> str:
        return self._shown_id

    def pending_identifier(self) -> str:
        return self._pending_id

    def is_armed(self) -> bool:
        return self._dwell.isActive()

    def is_disarmed(self) -> bool:
        return self._disarmed

    def popover(self) -> Optional[PreviewPopover]:
        return self._popover

    def opened_by_keyboard(self) -> bool:
        return self._keyboard_open

    def has_active_timer(self) -> bool:
        return (
            self._dwell.isActive()
            or self._closer.isActive()
            or self._slow.isActive()
        )

    # -- dismissal ---------------------------------------------------------

    def close(self, *, disarm: bool = False) -> None:
        self._dwell.stop()
        self._closer.stop()
        self._slow.stop()
        self._pending_id = ""
        # Dropping the correlation is what stops a slow image landing
        # after a swap and painting itself into the wrong draft.
        self._image_url = ""
        self._shown_id = ""
        if disarm:
            self._disarmed = True
        keyboard, self._keyboard_open = self._keyboard_open, False
        if self._popover is not None:
            was_visible = self._popover.isVisible()
            self._popover.close_preview()
            if keyboard and was_visible:
                self._return_focus()

    def disarm(self) -> None:
        """Close, and arm nothing until the next real move inside a row."""
        self.close(disarm=True)

    def shutdown(self) -> None:
        """Stop every timer and destroy the window. Nothing outlives this."""
        self.close()
        self._disconnect_loader()
        self._unwatch_window()
        if self._popover is not None:
            try:
                self._popover.dismissed.disconnect(self._on_dismissed)
            except (RuntimeError, TypeError):
                pass
            self._popover.hide()
            # Unparented rather than deleteLater'd: a deferred deletion
            # that never gets an event loop leaves a live window holding
            # a stale parent pointer, which is a crash waiting for the
            # next garbage collection. Unparenting hands ownership back
            # to Python, so dropping the reference destroys it here.
            self._popover.setParent(None)
            self._popover = None

    # -- store signals -----------------------------------------------------

    def on_record_changed(self, identifier: str) -> None:
        """Re-render in place, or close when the draft stopped qualifying."""
        # The body may have changed under the same identifier, so the
        # cached digest for it has to go before anything reads it again.
        self._forget_digest(identifier)
        if identifier != self._shown_id:
            # LOADING to READY under a resting pointer: eligibility is
            # re-evaluated here and the dwell arms from this moment, so
            # the user does not have to move off the row and back.
            if (
                identifier == self._hovered_id
                and not self._disarmed
                and not self._dwell.isActive()
                and self._eligible(identifier)
            ):
                self._pending_id = identifier
                self._pending_anchor = self._hovered_anchor
                self._dwell.start(PREVIEW_OPEN_DELAY_MS)
            return
        record = self._panel.preview_record(identifier)
        if record is None or not self._eligible_record(record):
            self.close()
            return
        # Keeps the anchor and the reserved image box; a height change
        # animates over the swap duration rather than jumping.
        if not self._render(record, self._shown_anchor, animate=True):
            self.close()

    def on_record_removed(self, identifier: str) -> None:
        self._forget_digest(identifier)
        if identifier in (self._shown_id, self._pending_id):
            self.close()

    def on_store_cleared(self) -> None:
        self._image_failures.clear()
        self._digests.clear()
        self.close()

    def on_search_changed(self) -> None:
        # ``_rebuild_list`` destroys every row widget, so an open popover
        # would be anchored to a dead object, and a stationary pointer
        # would otherwise sit over whichever row slid under it.
        self.disarm()

    def _forget_digest(self, identifier: str) -> None:
        for key in [k for k in self._digests if k[0] == identifier]:
            self._digests.pop(key, None)

    # -- events ------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if self._popover is not None and obj is self._popover:
            return self._on_popover_event(kind)
        if kind == QEvent.WindowDeactivate and obj is self._watched_window:
            # A keyboard-opened popover is what took the activation, so
            # that deactivation is not the user looking elsewhere.
            if not self._keyboard_open:
                self.close(disarm=True)
            return False
        if kind == QEvent.KeyPress and obj is getattr(self._panel, "_list", None):
            return self._on_list_key(event)
        identifier = self._row_identifier(obj)
        if identifier is None:
            return False
        if kind == QEvent.MouseMove:
            self._on_row_move(identifier, obj, event)
        elif kind == QEvent.Enter:
            # Records where the pointer is and nothing else. Arming is
            # left to MouseMove, which Qt delivers straight after an
            # Enter, so a list scrolling under a still pointer cannot
            # open anything.
            self._hovered_id = identifier
            self._hovered_anchor = self._anchor_of(obj)
        elif kind == QEvent.Leave:
            self._on_row_leave()
        elif kind == QEvent.MouseButtonPress:
            # ``popovers.md``: "a popover generally closes when people
            # click or tap outside its bounds".
            self.close(disarm=True)
        elif kind == QEvent.MouseButtonRelease:
            self._disarmed = False
        elif kind == QEvent.Wheel:
            self.close(disarm=True)
        elif kind == QEvent.ToolTip:
            return self._suppresses_tooltip(identifier)
        return False

    def _suppresses_tooltip(self, identifier: str) -> bool:
        """Whether the preview, and not the row's tooltip, speaks for a row.

        The popover deliberately does not cover the row it describes, so
        the pointer stays on the row and Qt's help event always follows:
        ``SH_ToolTip_WakeUpDelay`` is 700 ms on both the macOS and the
        Fusion style against a 500 ms dwell, and 20 ms when a tooltip was
        shown moments ago. Swallowing the event here is what keeps the
        row's tooltip from repeating the title, the host and the save
        time beside the surface already showing them. ``popovers.md``:
        "Don't show another view over a popover. Make sure nothing
        displays on top of a popover, except for an alert."

        Returning ``True`` suppresses it, and ``QApplication::notify``
        stops walking the parent chain at an accepted help event, so the
        list's own item tooltip does not step in behind it.

        A row that gets no preview keeps its tooltip: on a LOADING or
        FAILED draft, or a note whose row already shows its whole body,
        the tooltip is the only place the full text exists.
        """
        if identifier in (self._shown_id, self._pending_id):
            return True
        return self._eligible(identifier)

    def _on_popover_event(self, kind) -> bool:
        """The pointer reaching, resting in, and leaving the surface itself.

        Reaching it is the whole point of the grace period: the popover
        sits across the panel's border and an 8 px gap, and during that
        crossing the pointer is over neither the row nor the window.
        """
        if kind == QEvent.Enter:
            self._closer.stop()
        elif kind == QEvent.Leave:
            if self.is_open():
                # Short, because leaving is deliberate; long enough that
                # a jitter at its own edge while reading does not close
                # it.
                self._closer.start(PREVIEW_CLOSE_DELAY_MS)
        elif kind == QEvent.MouseButtonPress:
            self.close(disarm=True)
        return False

    def _row_identifier(self, obj) -> Optional[str]:
        if not isinstance(obj, QWidget):
            return None
        identifier = obj.property("draft_identifier")
        return identifier if isinstance(identifier, str) and identifier else None

    def _on_row_move(self, identifier: str, row: QWidget, event) -> None:
        if event.buttons() != Qt.NoButton:
            # A drag is not a dwell.
            return
        self._disarmed = False
        self._closer.stop()
        anchor = self._anchor_of(row)
        if identifier == self._shown_id:
            self._shown_anchor = anchor
            return
        if identifier == self._pending_id and self._dwell.isActive():
            # Movement inside one row must not restart the dwell, or a
            # hand that is not perfectly still never reaches 500 ms.
            self._pending_anchor = anchor
            return
        if not self._eligible(identifier):
            self._dwell.stop()
            self._pending_id = ""
            if self.is_open():
                self._closer.start(PREVIEW_LEAVE_GRACE_MS)
            return
        self._pending_id = identifier
        self._pending_anchor = anchor
        # A swap keeps one window on screen and only costs the shorter
        # delay; a cold open pays the full dwell.
        self._dwell.start(
            PREVIEW_SWAP_DELAY_MS if self.is_open() else PREVIEW_OPEN_DELAY_MS
        )

    def _on_row_leave(self) -> None:
        self._dwell.stop()
        self._pending_id = ""
        self._hovered_id = ""
        if self.is_open():
            # The grace period the pointer needs to cross the panel's
            # border and the 8 px gap into the popover itself.
            self._closer.start(PREVIEW_LEAVE_GRACE_MS)

    def _on_list_key(self, event) -> bool:
        if event.key() != Qt.Key_Space or event.modifiers() != Qt.NoModifier:
            return False
        # Space because that is the system's own preview key, in Finder
        # and in Quick Look. ``accessibility.md``: "Prefer system
        # gestures and behaviors people are already familiar with over
        # creating custom gestures people must learn and retain." The
        # list's items are not checkable, so nothing is taken away.
        self.toggle_current_row()
        return True

    def _on_scrolled(self, _value: int) -> None:
        # Without this a stationary pointer plus a wheel flick opens a
        # preview for whatever row happened to slide underneath it.
        self.close(disarm=True)

    def _on_dwell(self) -> None:
        identifier, anchor = self._pending_id, self._pending_anchor
        self._pending_id = ""
        if not identifier or self._disarmed:
            return
        self.open_for(identifier, anchor)

    def _on_dismissed(self) -> None:
        self.close()

    # -- opening -----------------------------------------------------------

    def toggle_current_row(self) -> bool:
        """The keyboard route: Space on the focused row toggles the preview."""
        identifier, anchor = self._panel.preview_current_row()
        if not identifier:
            return False
        if self._shown_id == identifier and self.is_visible():
            self.close()
            return False
        return self.open_for(identifier, anchor, keyboard=True)

    def open_for(
        self, identifier: str, anchor: QRect, *, keyboard: bool = False,
    ) -> bool:
        """Show the preview for one draft. Returns whether it opened."""
        if not self._panel.preview_is_available():
            return False
        record = self._panel.preview_record(identifier)
        if record is None or not self._eligible_record(record):
            return False
        swap = self.is_open() and not keyboard
        self._closer.stop()
        if not self._render(record, anchor, animate=swap):
            self.close()
            return False
        self._shown_id = identifier
        self._shown_anchor = anchor
        self._keyboard_open = bool(keyboard)
        popover = self._ensure_popover()
        if not swap:
            popover.show_preview(keyboard=keyboard)
        self._start_image()
        return True

    def _render(self, record: DraftRecord, anchor: QRect, *, animate: bool) -> bool:
        popover = self._ensure_popover()
        popover.set_images_enabled(self._loader is not None)
        available = self._available_rect(anchor)
        width = preview_width(available)
        digest = self._digest(record)
        size = popover.compose(
            record,
            width=width,
            max_height=preview_max_height(available),
            now=self._now(),
            digest=digest,
        )
        placement = (
            preview_geometry(anchor, size, available) if size is not None else None
        )
        if placement is None and size is not None:
            # One retry on a shorter window: neither side had room, and
            # a shorter surface can still fit above or below the row.
            size = popover.compose(
                record,
                width=width,
                max_height=max(120, size.height() // 2),
                now=self._now(),
                digest=digest,
            )
            placement = (
                preview_geometry(anchor, size, available) if size is not None else None
            )
        if placement is None:
            return False
        popover.place(placement, animate=animate)
        return True

    def _ensure_popover(self) -> PreviewPopover:
        if self._popover is None:
            self._popover = PreviewPopover(
                self._panel.window(),
                is_dark=self._is_dark,
                child_mode=uses_child_window(QGuiApplication.platformName()),
            )
            self._popover.dismissed.connect(self._on_dismissed)
            self._popover.installEventFilter(self)
        self._watch_window()
        return self._popover

    def _watch_window(self) -> None:
        window = self._panel.window()
        if window is self._watched_window:
            return
        self._unwatch_window()
        if window is not None:
            window.installEventFilter(self)
            self._watched_window = window

    def _unwatch_window(self) -> None:
        if self._watched_window is None:
            return
        try:
            self._watched_window.removeEventFilter(self)
        except RuntimeError:
            pass
        self._watched_window = None

    def _return_focus(self) -> None:
        # Escape leaves the current row exactly where it was; only the
        # focus comes back.
        list_widget = getattr(self._panel, "_list", None)
        if list_widget is not None:
            list_widget.setFocus(Qt.OtherFocusReason)

    # -- image -------------------------------------------------------------

    def _start_image(self) -> None:
        """One fetch per popover, started at open, never at dwell start.

        Never prefetched for rows that were not hovered, and never at
        all for a draft that is still encrypted: an eligible record is
        READY by definition, and a LOADING or FAILED one never gets
        here.
        """
        self._slow.stop()
        self._image_url = ""
        popover = self._popover
        if popover is None or not popover.reserved_image():
            return
        url = popover.fields().image_url
        if not url or self._loader is None or url in self._image_failures:
            # Nothing is said about a refused or absent image, because
            # then no box was reserved at all: the user did not ask for
            # an image and cannot act on the refusal. A box that was
            # reserved and cannot be filled is the only case that speaks.
            popover.set_image_note(_IMAGE_UNAVAILABLE)
            return
        self._image_url = url
        self._slow.start(PREVIEW_IMAGE_SLOW_MS)
        self._loader.load_url(url)

    def _on_image_slow(self) -> None:
        if self._image_url and self._popover is not None:
            # ``loading.md``: "Show something as soon as possible... show
            # placeholder text, graphics, or animations as content loads,
            # replacing these elements as content becomes available."
            self._popover.set_image_note(_IMAGE_WAITING)

    def _on_image_ready(self, url: str, _sha: str, pixmap) -> None:
        if url != self._image_url or self._popover is None:
            return
        self._slow.stop()
        self._popover.set_image_pixmap(pixmap)

    def _on_image_failed(self, url: str, reason: str) -> None:
        self._image_failures[url] = reason or "unavailable"
        if url != self._image_url or self._popover is None:
            return
        self._slow.stop()
        self._popover.set_image_note(_IMAGE_UNAVAILABLE)

    # -- helpers -----------------------------------------------------------

    def _digest(self, record: DraftRecord) -> BodyDigest:
        key = (record.identifier, len(record.content or ""))
        digest = self._digests.get(key)
        if digest is None:
            digest = body_digest(record.content)
            self._digests[key] = digest
        return digest

    def _eligible(self, identifier: str) -> bool:
        record = self._panel.preview_record(identifier)
        return record is not None and self._eligible_record(record)

    def _eligible_record(self, record: DraftRecord) -> bool:
        return preview_is_eligible(
            record, now=self._now(), digest=self._digest(record),
        )

    def _anchor_of(self, row: QWidget) -> QRect:
        return QRect(row.mapToGlobal(row.rect().topLeft()), row.size())

    def shown_anchor(self) -> QRect:
        return QRect(self._shown_anchor)

    def _available_rect(self, anchor: QRect) -> QRect:
        if self._popover is not None and self._popover.is_child_window():
            window = self._panel.window()
            if window is not None:
                return QRect(window.mapToGlobal(QPoint(0, 0)), window.size())
        return screen_available_rect(anchor, self._panel)

    def _now(self) -> int:
        return self._panel.preview_now()
