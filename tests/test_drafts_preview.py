# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guarantees for the drafts panel's hover preview.

The defect classes this file guards against, in the order they cost the
most:

- a popover that opens on rows the pointer only crossed, or that never
  opens because every twitch restarts its dwell;
- a popover that covers the row it describes, lands off screen, or lands
  under the Dock, on any one of several monitors;
- a hero image that reflows the window when it arrives, or that fires a
  request at the user's own machine because a feed said so;
- a timer or a signal that outlives the panel, the record or the
  profile it belonged to;
- hover as the only route to the information, which an accessibility
  audit of this panel has already flagged once.

No test here touches a network, a relay, a signer or the real
``~/.config``: the image path is injected, and with nothing injected the
preview constructs no request at all.
"""

from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QHelpEvent, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QToolTip, QVBoxLayout, QWidget

from nostr.draft_store import DraftRecord, DraftState, DraftStore
from nostr.drafts import DraftWrapMeta
from nostr.profiles import Profile
from nostr.ui import drafts_preview as dp
from nostr.ui.drafts_common import THEME_TOKENS
from nostr.ui.drafts_panel import DraftsPanel, _accessible_row_text
from nostr.ui.thumbnail_loader import ThumbnailLoader
from tests.blossom_fakes import FakeNam, FakeReply


PK = "a" * 64
NOW = 1_750_000_000
HERO = "https://cdn.example.com/hero.png"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def app_font():
    """Restore the application font, it is process-wide state."""
    app = QApplication.instance()
    original = app.font()
    yield app
    app.setFont(original)


@pytest.fixture
def tooltips():
    """Clean the shared tooltip state, it is process-wide like the font."""
    QToolTip.hideText()
    yield QToolTip
    QToolTip.hideText()


@pytest.fixture
def real_timing():
    """Opt one test out of :func:`instant_timing`.

    Requesting this fixture leaves the shipped delays in place, so a
    test can assert what the application actually waits rather than what
    a monkeypatch just set. Nothing here sleeps: the intervals are read
    off the armed timers.
    """
    yield


@pytest.fixture(autouse=True)
def instant_timing(request, monkeypatch):
    """Drive every delay from the test, never from the clock.

    The timers are still armed and still have to be fired, so the tests
    below can tell "armed and waiting" from "already fired"; only the
    waiting costs nothing.
    """
    if "real_timing" in request.fixturenames:
        yield
        return
    for name in (
        "PREVIEW_OPEN_DELAY_MS", "PREVIEW_SWAP_DELAY_MS",
        "PREVIEW_LEAVE_GRACE_MS", "PREVIEW_CLOSE_DELAY_MS",
        "PREVIEW_FADE_MS", "PREVIEW_IMAGE_SLOW_MS",
    ):
        monkeypatch.setattr(dp, name, 0)
    yield


# --------------------------------------------------------------------------- #
# Fixtures for records                                                        #
# --------------------------------------------------------------------------- #

def record(**kwargs) -> DraftRecord:
    base = dict(
        identifier="d0",
        inner_kind=30023,
        state=DraftState.READY,
        title="One Class, One Purpose",
        snippet="A short row snippet",
        content="body " * 300,
        inner_tags=[],
        created_at=NOW - 86_400,
    )
    base.update(kwargs)
    return DraftRecord(**base)


def article(**kwargs) -> DraftRecord:
    """The common case: an imported NIP-23 article with every field."""
    tags = [
        ["source", "https://www.blog.example.com/posts/1"],
        ["summary", "Refactoring a whole import pipeline without breaking it."],
        ["image", HERO],
        ["published_at", str(NOW - 30 * 86_400)],
        ["t", "python"],
        ["t", "qt"],
    ]
    kwargs.setdefault("inner_tags", tags)
    return record(**kwargs)


def note(body: str) -> DraftRecord:
    """A short note, whose row already shows its whole body."""
    from nostr.drafts import derive_preview_snippet, derive_title_from_markdown

    return record(
        inner_kind=1,
        title=derive_title_from_markdown(body, max_len=60, fallback="Untitled note"),
        snippet=derive_preview_snippet(body),
        content=body,
        inner_tags=[],
    )


# --------------------------------------------------------------------------- #
# Eligibility and content, no window needed                                   #
# --------------------------------------------------------------------------- #

def test_a_ready_article_is_eligible_and_a_loading_one_is_not():
    assert dp.preview_is_eligible(article(), now=NOW) is True
    assert dp.preview_is_eligible(
        article(state=DraftState.LOADING), now=NOW) is False
    assert dp.preview_is_eligible(
        article(state=DraftState.FAILED, failure_reason="signer timed out"),
        now=NOW,
    ) is False


def test_a_note_the_row_already_shows_whole_opens_nothing():
    # The row's snippet is the whole body, so a 360 px window would
    # repeat it verbatim. popovers.md: "Avoid making a popover too big."
    assert dp.preview_is_eligible(note("Pineapple notes"), now=NOW) is False
    # A longer body has something left to show.
    assert dp.preview_is_eligible(note("word " * 200), now=NOW) is True


def test_every_missing_field_drops_its_line_rather_than_printing_a_placeholder():
    bare = record(title="", content="", snippet="", inner_tags=[], created_at=0)
    fields = dp.preview_fields(bare, now=NOW)
    assert fields.title == "(no title)"
    for name in ("summary", "origin", "stats", "tags", "expiry", "image_url"):
        assert getattr(fields, name) == "", name
    # And no field ever carries an apologetic string.
    for value in fields:
        assert "no summary" not in value.lower()
        assert "unknown" not in value.lower()


def test_the_origin_line_carries_the_host_the_row_carries():
    fields = dp.preview_fields(article(), now=NOW)
    parts = fields.origin.split(" · ")
    assert parts[0] == "blog.example.com"
    assert parts[1].startswith("Published ")
    assert parts[2].startswith("Saved ")


def test_a_draft_published_and_saved_in_the_same_minute_shows_one_date():
    # Two identical dates on one line is noise.
    same = article(created_at=NOW, inner_tags=[
        ["source", "https://blog.example.com/x"],
        ["published_at", str(NOW - 30)],
    ])
    origin = dp.preview_fields(same, now=NOW).origin
    assert "Published" not in origin
    assert origin.count("Saved") == 1

    apart = article(created_at=NOW, inner_tags=[
        ["source", "https://blog.example.com/x"],
        ["published_at", str(NOW - 600)],
    ])
    assert "Published" in dp.preview_fields(apart, now=NOW).origin


def test_reading_stats_ignore_markdown_syntax_and_image_urls():
    body = (
        "# One Class, One Purpose\n\n"
        "![a very long alt text](https://cdn.example.com/a/very/long/hero.png)\n\n"
        "**Three** more [words](https://example.com/somewhere/deep)\n"
    )
    words, minutes = dp.reading_stats(body)
    # "One Class, One Purpose" (4) + "Three more words" (3).
    assert words == 7
    assert minutes == 1
    assert "https" not in dp.body_digest(body).opening


def test_reading_time_rounds_up_and_never_reads_zero():
    assert dp.reading_stats("word " * 201) == (201, 2)
    assert dp.reading_stats("word " * 200) == (200, 1)
    assert dp.reading_stats("") == (0, 0)


def test_the_stats_line_shows_both_time_and_words():
    fields = dp.preview_fields(record(content="word " * 1240), now=NOW)
    assert fields.stats == "7 min read · 1,240 words"


def test_hashtags_dedupe_case_insensitively_and_cap_at_six():
    tags = [["t", "Bitcoin"], ["t", "bitcoin"], ["t", "BITCOIN"]]
    assert dp.article_hashtags(record(inner_tags=tags)) == ["Bitcoin"]

    many = [["t", f"tag{i}"] for i in range(9)]
    line = dp.preview_fields(record(inner_tags=many), now=NOW).tags
    assert line.startswith("#tag0 · ")
    assert line.endswith("+3 more")
    assert line.count("#") == 6


def test_expiry_appears_inside_the_horizon_and_not_outside_it():
    assert dp.expiry_phrase(NOW + 13 * 86_400, now=NOW) == "Expires in 13 days"
    assert dp.expiry_phrase(NOW + 15 * 86_400, now=NOW) == ""
    assert dp.expiry_phrase(NOW + 3600, now=NOW) == "Expires today"
    assert dp.expiry_phrase(NOW + 86_400 + 60, now=NOW) == "Expires in 1 day"
    assert dp.expiry_phrase(None, now=NOW) == ""
    # The word carries the meaning, not the colour. accessibility.md:
    # "Convey information with more than color alone."
    assert "Expire" in dp.expiry_phrase(NOW + 86_400, now=NOW)


def test_an_imminent_expiry_alone_makes_a_thin_note_worth_previewing():
    thin = note("Pineapple notes")
    assert dp.preview_is_eligible(thin, now=NOW) is False
    thin.expiration = NOW + 2 * 86_400
    assert dp.preview_is_eligible(thin, now=NOW) is True


def test_the_summary_prefers_the_tag_and_never_the_row_snippet():
    tagged = dp.preview_fields(article(), now=NOW).summary
    assert tagged.startswith("Refactoring a whole import pipeline")

    untagged = article(inner_tags=[["image", HERO]], content="Real body words.")
    fields = dp.preview_fields(untagged, now=NOW)
    assert fields.summary == "Real body words."
    assert fields.summary != untagged.snippet


def test_a_very_long_body_is_capped_before_it_reaches_the_layout():
    digest = dp.body_digest("word " * 20_000)
    assert len(digest.opening) <= 401
    assert digest.opening.endswith("…")


# --------------------------------------------------------------------------- #
# The image gate, before any window exists                                    #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8080/hero.png",     # loopback, which the media policy allows
    "http://blog.example.com/hero.png",   # plain http, which the mirror policy allows
    "https://user:pw@blog.example/x.png",  # userinfo
    "https://10.0.0.1/hero.png",          # private IP literal
    "https://169.254.169.254/hero.png",   # link-local metadata service
    "file:///etc/passwd",
    "data:image/png;base64,AAAA",
    "",
])
def test_a_refused_hero_url_is_indistinguishable_from_an_absent_one(url):
    # The intersection of two policies: https only, no userinfo, no
    # private or link-local IP literals. Neither policy alone is enough.
    assert dp.article_image_url(record(inner_tags=[["image", url]])) == ""


def test_an_https_hero_on_a_named_host_passes_the_gate():
    assert dp.article_image_url(record(inner_tags=[["image", HERO]])) == HERO


# --------------------------------------------------------------------------- #
# Placement, table driven against synthetic rects                             #
# --------------------------------------------------------------------------- #

SCREEN = QRect(0, 0, 1440, 900)
CARD = QSize(360, 400)


def test_a_right_docked_panel_places_the_popover_to_the_left():
    row = QRect(1120, 400, 320, 54)
    placement = dp.preview_geometry(row, CARD, SCREEN)
    assert placement.side == "left"
    assert placement.rect.right() < row.left()
    # The arrow points back at the row it came from. popovers.md: "Make
    # sure a popover's arrow points as directly as possible to the
    # element that revealed it."
    assert placement.arrow >= 0
    assert placement.rect.top() + placement.arrow == row.center().y()


def test_the_left_side_wins_whenever_both_sides_fit():
    # The row above is flush against the right edge, so the right side
    # never fits there and the case cannot tell a preference from an
    # accident. In an unmaximised window both sides fit, and left has to
    # win: the panel docks at the right edge, so left is the side that
    # points back into the editor rather than off the screen.
    row = QRect(700, 400, 320, 54)
    placement = dp.preview_geometry(row, CARD, SCREEN)
    assert placement.side == "left"
    assert placement.rect.right() < row.left()


def test_no_room_on_the_left_flips_to_the_right():
    row = QRect(0, 400, 320, 54)
    placement = dp.preview_geometry(row, CARD, SCREEN)
    assert placement.side == "right"
    assert placement.rect.left() > row.right()


def test_neither_side_flips_below_and_then_above():
    narrow = QRect(0, 0, 400, 900)
    row = QRect(20, 100, 360, 54)
    below = dp.preview_geometry(row, CARD, narrow)
    assert below.side == "below"
    assert below.rect.top() > row.bottom()

    low = QRect(20, 800, 360, 54)
    above = dp.preview_geometry(low, CARD, narrow)
    assert above.side == "above"
    assert above.rect.bottom() < low.top()


@pytest.mark.parametrize("screen,row", [
    (SCREEN, QRect(1120, 400, 320, 54)),          # left
    (SCREEN, QRect(0, 400, 320, 54)),             # right
    (QRect(0, 0, 400, 900), QRect(20, 100, 360, 54)),   # below
    (QRect(0, 0, 400, 900), QRect(20, 800, 360, 54)),   # above
    (QRect(0, 25, 1440, 800), QRect(1120, 810, 320, 54)),  # clamped hard down
])
def test_the_popover_never_covers_the_row_it_describes(screen, row):
    # popovers.md: "Ideally, a popover doesn't cover the element that
    # revealed it or any essential content people may need to see while
    # using it."
    placement = dp.preview_geometry(row, CARD, screen)
    assert placement is not None
    assert not placement.rect.intersects(row), placement.side
    assert screen.contains(placement.rect), placement.side


def test_a_second_screen_with_a_negative_origin_still_contains_the_popover():
    # A 4K monitor to the left of the laptop screen: the origin is
    # negative and every clamp has to be computed against it, not
    # against zero.
    left_screen = QRect(-2560, -200, 2560, 1440)
    row = QRect(-400, -180, 320, 54)
    placement = dp.preview_geometry(row, CARD, left_screen)
    assert left_screen.contains(placement.rect)
    assert placement.rect.top() >= left_screen.top()


def test_the_clamp_uses_the_available_rect_and_not_the_full_screen():
    # layout.md, macOS: "Avoid placing controls or critical information
    # at the bottom of a window." The screen equivalent is never trusting
    # the anchor to be somewhere sane: a menu bar at the top and a Dock
    # at the bottom are carved out of the available rect.
    available = QRect(0, 25, 1440, 800)
    row = QRect(1120, 810, 320, 54)
    placement = dp.preview_geometry(row, CARD, available)
    assert available.contains(placement.rect)


def test_an_arrow_that_would_leave_its_band_is_dropped_rather_than_stubbed():
    # A stub pointing at nothing is worse than no arrow: a tall popover
    # against a row at the very top of the screen has been clamped so
    # far that its arrow would sit on the corner radius.
    available = QRect(0, 25, 1440, 800)
    top_row = QRect(1120, 25, 320, 20)
    placement = dp.preview_geometry(top_row, QSize(360, 700), available)
    assert placement.side == "left"
    assert placement.arrow == -1
    assert available.contains(placement.rect)

    # A row in the middle of the same screen keeps its arrow.
    middle = dp.preview_geometry(QRect(1120, 400, 320, 54), CARD, available)
    assert middle.arrow >= 0


def test_nothing_fits_yields_no_preview_at_all():
    tiny = QRect(0, 0, 300, 200)
    assert dp.preview_geometry(QRect(10, 10, 100, 40), CARD, tiny) is None


def test_the_width_scales_with_the_font_and_stays_clamped(app_font):
    widths = {}
    for point_size in (9, 13, 26):
        font = QFont(app_font.font())
        font.setPointSizeF(point_size)
        app_font.setFont(font)
        widths[point_size] = dp.preview_width(SCREEN)
    assert widths[26] > widths[13]
    assert 320 <= widths[9] <= 460
    assert 320 <= widths[26] <= 460
    # And it never outgrows a small screen.
    assert dp.preview_width(QRect(0, 0, 300, 400)) <= 300


def test_wayland_is_the_one_platform_that_gets_a_child_window():
    assert dp.uses_child_window("wayland") is True
    assert dp.uses_child_window("wayland-egl") is True
    assert dp.uses_child_window("cocoa") is False
    assert dp.uses_child_window("windows") is False
    assert dp.uses_child_window("offscreen") is False
    assert dp.uses_child_window("") is False


# --------------------------------------------------------------------------- #
# Composition and the degrade ladder                                          #
# --------------------------------------------------------------------------- #

def compose(
    rec, *, width=360, max_height=460, is_dark=True, images=True,
) -> dp.PreviewPopover:
    popover = dp.PreviewPopover(is_dark=is_dark)
    popover.set_images_enabled(images)
    popover.compose(rec, width=width, max_height=max_height, now=NOW)
    return popover


def test_a_full_article_composes_with_everything_in_reading_order(app_font):
    font = QFont(app_font.font())
    font.setPointSizeF(13)
    app_font.setFont(font)

    popover = compose(article())
    assert popover.plan() == dp.LADDER[0]
    assert popover.reserved_image() is True
    order = [name for name, label in popover.labels() if label.height()]
    assert order == ["title", "summary", "origin", "stats", "tags"]
    assert popover.card_size().height() <= 460


def test_the_degrade_ladder_runs_in_the_documented_order(app_font):
    font = QFont(app_font.font())
    font.setPointSizeF(13)
    app_font.setFont(font)
    rec = article(inner_tags=[
        ["source", "https://blog.example.com/x"],
        ["summary", "A long summary. " * 30],
        ["image", HERO],
        ["published_at", str(NOW - 30 * 86_400)],
        ["t", "python"], ["t", "qt"],
    ])

    seen = []
    for max_height in (600, 420, 380, 340, 240, 120):
        popover = dp.PreviewPopover(is_dark=True)
        popover.set_images_enabled(True)
        size = popover.compose(rec, width=360, max_height=max_height, now=NOW)
        if size is None:
            seen.append(None)
            continue
        assert size.height() <= max_height
        seen.append(popover.plan())

    rungs = [p for p in seen if p is not None]
    # Summary shrinks first, then the hashtag line goes, then the hero.
    assert rungs == sorted(rungs, key=lambda p: (-p.summary_lines, -p.tags, -p.image))
    assert rungs[0].summary_lines >= rungs[-1].summary_lines
    assert any(p.tags is False for p in rungs)
    assert any(p.image is False for p in rungs)
    # The title and the origin line are never dropped.
    assert all(p.summary_lines >= 2 for p in rungs)


def test_a_composition_that_cannot_fit_at_all_returns_nothing():
    popover = dp.PreviewPopover(is_dark=True)
    popover.set_images_enabled(True)
    assert popover.compose(article(), width=360, max_height=10, now=NOW) is None


def test_a_clamped_line_elides_rather_than_clipping():
    # The bug _ElidingLabel exists to prevent: a hard-clipped last line
    # with no sign anything was cut.
    popover = compose(article(inner_tags=[
        ["summary", "A long summary that keeps going. " * 20],
        ["image", HERO],
    ]))
    summary = dict(popover.labels())["summary"]
    assert summary.is_truncated() is True
    assert summary.line_count() <= 5
    lines = summary.painted_lines()
    assert lines[-1].endswith("…")
    # And the whole string is still reachable, because it never went
    # through setText.
    assert summary.text() == ""
    assert summary.accessibleName() == popover.fields().summary


def test_the_origin_line_wraps_rather_than_eliding():
    long_origin = article(inner_tags=[
        ["source", "https://a-fairly-long-host.example.com/posts/1"],
        ["published_at", str(NOW - 30 * 86_400)],
        ["image", HERO],
    ])
    popover = compose(long_origin)
    origin = dict(popover.labels())["origin"]
    assert origin.line_count() == 2
    assert origin.is_truncated() is False


@pytest.mark.parametrize("is_dark", [True, False])
@pytest.mark.parametrize("point_size", [13, 26])
def test_the_popover_paints_at_both_type_sizes_in_both_themes(
    app_font, point_size, is_dark,
):
    # accessibility.md: "give people the option to enlarge text by at
    # least 200 percent".
    font = QFont(app_font.font())
    font.setPointSizeF(point_size)
    app_font.setFont(font)

    available = QRect(0, 0, 1440, 900)
    popover = dp.PreviewPopover(is_dark=is_dark)
    size = popover.compose(
        article(), width=dp.preview_width(available),
        max_height=dp.preview_max_height(available), now=NOW,
    )
    assert size is not None
    popover.place(dp.preview_geometry(QRect(1120, 400, 320, 54), size, available))
    assert not popover.grab().isNull()
    # The title and the origin survive every rung of the ladder.
    labels = dict(popover.labels())
    assert labels["title"].height() > 0
    assert labels["origin"].height() > 0


# --------------------------------------------------------------------------- #
# Panel wiring                                                                #
# --------------------------------------------------------------------------- #

def wrap_meta(identifier: str, *, created_at: int = NOW - 86_400):
    return DraftWrapMeta(
        identifier=identifier,
        inner_kind=30023,
        event_id=identifier + "e",
        pubkey=PK,
        created_at=created_at,
        expiration=None,
        ciphertext="ciphertext",
    )


def hero_url(index: int) -> str:
    """One hero per draft, so a correlation bug cannot pass by accident."""
    return f"https://cdn.example.com/hero-{index}.png"


def article_inner(index: int, *, image: bool = True):
    tags = [
        ["title", f"Article {index}"],
        ["source", "https://blog.example.com/x"],
        ["summary", "A summary long enough to be worth a window. " * 3],
        ["published_at", str(NOW - 30 * 86_400)],
        ["t", "python"],
    ]
    if image:
        tags.append(["image", hero_url(index)])
    return {"kind": 30023, "content": "body " * 300, "tags": tags}


def populated_store(count: int = 3, *, image: bool = True) -> DraftStore:
    store = DraftStore()
    store.bind_profile(PK)
    for i in range(count):
        store.upsert_skeleton(wrap_meta(f"d{i}", created_at=NOW - 86_400 + i))
        store.set_decrypted(f"d{i}", inner=article_inner(i, image=image))
    return store


def make_profile() -> Profile:
    return Profile(
        user_pubkey=PK,
        bunker_pubkey="b" * 64,
        bunker_relays=["wss://relay.example"],
        local_secret_hex="c" * 64,
        display_name="Alice",
    )


@pytest.fixture
def panel(request):
    """A shown, bound panel with a frozen clock, hidden on the way out."""
    store = getattr(request, "param", None) or populated_store()
    widget = DraftsPanel(is_dark=True)
    widget.set_active_profile(make_profile())
    widget.bind_store(store)
    widget.preview_now = lambda: NOW
    widget.resize(320, 600)
    widget.show()
    widget.activateWindow()
    QApplication.processEvents()
    widget._store_ref = store
    yield widget
    # Torn down here rather than left to a garbage collection that runs
    # after the QApplication has gone: the preview is a second top-level
    # window, and a window outliving its owner is the exact defect the
    # lifetime tests below are about.
    widget._preview.shutdown()
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.fixture
def child_host(panel):
    """The panel as a child widget, the way ``main_window`` docks it.

    In the application the panel is added to the central splitter and
    hidden with ``hide()``, so it is a child widget and not a window:
    hiding it delivers no ``WindowDeactivate`` to anything, and its own
    ``hideEvent`` is the only thing left that can take the popover down.

    Torn down before the ``panel`` fixture, and it hands the panel back
    to itself first so the panel outlives the host it was borrowed by.
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(panel)
    host.resize(900, 700)
    host.show()
    host.activateWindow()
    QApplication.processEvents()
    yield host
    panel.setParent(None)
    host.hide()
    host.deleteLater()


def row_of(panel, index: int):
    return panel._list.itemWidget(panel._list.item(index))


def identifier_at(panel, index: int) -> str:
    return panel._list.item(index).data(Qt.UserRole)


def index_of(panel, identifier: str) -> int:
    """The list is newest first, so a row's index is not its number."""
    return next(
        i for i in range(panel._list.count())
        if identifier_at(panel, i) == identifier
    )


def move_onto(panel, index: int) -> None:
    """A real pointer move inside a row, as Qt would deliver it."""
    row = row_of(panel, index)
    QApplication.sendEvent(row, QEvent(QEvent.Enter))
    QApplication.sendEvent(row, QMouseEvent(
        QEvent.MouseMove, QPointF(5, 5), QPointF(5, 5),
        Qt.NoButton, Qt.NoButton, Qt.NoModifier,
    ))


def leave(panel, index: int) -> None:
    QApplication.sendEvent(row_of(panel, index), QEvent(QEvent.Leave))


def press(panel, index: int) -> None:
    QApplication.sendEvent(row_of(panel, index), QMouseEvent(
        QEvent.MouseButtonPress, QPointF(5, 5), QPointF(5, 5),
        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
    ))


def fire(timer) -> None:
    """Fire an armed timer now, instead of waiting out its interval."""
    assert timer.isActive(), "expected an armed timer"
    timer.stop()
    timer.timeout.emit()


def open_preview(panel, index: int = 0) -> None:
    move_onto(panel, index)
    fire(panel._preview._dwell)


# --------------------------------------------------------------------------- #
# Dwell timing                                                                #
# --------------------------------------------------------------------------- #

def test_resting_on_a_row_opens_the_preview_for_that_row(panel):
    controller = panel._preview
    assert controller.is_open() is False
    move_onto(panel, 1)
    assert controller.is_armed() is True
    assert controller.pending_identifier() == "d1"
    # Nothing is on screen until the dwell has actually elapsed.
    assert controller.is_open() is False
    fire(controller._dwell)
    assert controller.is_open() is True
    assert controller.shown_identifier() == "d1"


def test_the_shipped_delays_are_the_ones_the_timers_are_armed_with(
    panel, real_timing,
):
    """The half second the feature was asked for, read off the module.

    Every other test here runs with ``instant_timing`` holding the
    delays at zero, and the two that name an interval patch it first and
    then assert what they set. Without this test the dwell could be
    deleted outright, or stretched to five seconds, and the suite would
    stay green. ``real_timing`` opts this one out of the patch, and the
    intervals are read off armed timers, so nothing here sleeps.
    """
    controller = panel._preview
    # 500 ms is the "delay of 0.5 seconds" the preview was asked for.
    assert dp.PREVIEW_OPEN_DELAY_MS == 500
    assert dp.PREVIEW_SWAP_DELAY_MS == 120
    assert dp.PREVIEW_LEAVE_GRACE_MS == 200
    assert dp.PREVIEW_CLOSE_DELAY_MS == 150

    # And every one of them reaches the timer it belongs to.
    move_onto(panel, 1)
    assert controller._dwell.interval() == 500
    fire(controller._dwell)
    assert controller.is_open() is True

    move_onto(panel, 2)
    assert controller._dwell.interval() == 120
    fire(controller._dwell)

    leave(panel, 2)
    assert controller._closer.interval() == 200

    popover = controller.popover()
    QApplication.sendEvent(popover, QEvent(QEvent.Enter))
    QApplication.sendEvent(popover, QEvent(QEvent.Leave))
    assert controller._closer.interval() == 150


def test_movement_inside_one_row_does_not_restart_the_dwell(panel, monkeypatch):
    # A hand that is not perfectly still must still reach 500 ms.
    monkeypatch.setattr(dp, "PREVIEW_OPEN_DELAY_MS", 500)
    controller = panel._preview
    move_onto(panel, 1)
    started = controller._dwell.remainingTime()
    for _ in range(3):
        QApplication.sendEvent(row_of(panel, 1), QMouseEvent(
            QEvent.MouseMove, QPointF(9, 9), QPointF(9, 9),
            Qt.NoButton, Qt.NoButton, Qt.NoModifier,
        ))
    assert controller._dwell.remainingTime() <= started
    assert controller.pending_identifier() == "d1"


def test_a_cold_open_pays_the_full_dwell_and_a_swap_pays_the_short_one(
    panel, monkeypatch,
):
    monkeypatch.setattr(dp, "PREVIEW_OPEN_DELAY_MS", 500)
    monkeypatch.setattr(dp, "PREVIEW_SWAP_DELAY_MS", 120)
    controller = panel._preview
    move_onto(panel, 0)
    assert controller._dwell.interval() == 500
    fire(controller._dwell)

    move_onto(panel, 1)
    assert controller._dwell.interval() == 120


def test_moving_between_rows_swaps_one_window_rather_than_reopening(panel):
    controller = panel._preview
    open_preview(panel, 0)
    first = controller.popover()
    assert controller.shown_identifier() == identifier_at(panel, 0)

    leave(panel, 0)
    move_onto(panel, 1)
    fire(controller._dwell)
    assert controller.shown_identifier() == identifier_at(panel, 1)
    # One window for the panel's lifetime. popovers.md: "Show one popover
    # at a time."
    assert controller.popover() is first
    assert first.isVisible() is True


def test_leaving_a_row_closes_only_after_the_grace_period(panel):
    controller = panel._preview
    open_preview(panel, 0)
    leave(panel, 0)
    # Still open: the pointer may be crossing the gap to the popover.
    assert controller.is_open() is True
    fire(controller._closer)
    assert controller.is_open() is False


def test_entering_another_row_cancels_the_pending_close(panel):
    controller = panel._preview
    open_preview(panel, 0)
    leave(panel, 0)
    assert controller._closer.isActive() is True
    move_onto(panel, 1)
    assert controller._closer.isActive() is False


def test_reaching_the_popover_keeps_it_open(panel):
    # The gap the grace period exists for: the popover sits across the
    # panel's border and an 8 px gap, and during that crossing the
    # pointer is over neither the row nor the window.
    controller = panel._preview
    open_preview(panel, 0)
    leave(panel, 0)
    assert controller._closer.isActive() is True

    popover = controller.popover()
    QApplication.sendEvent(popover, QEvent(QEvent.Enter))
    assert controller._closer.isActive() is False
    assert controller.is_open() is True


def test_leaving_the_popover_closes_it_after_its_own_delay(panel, monkeypatch):
    monkeypatch.setattr(dp, "PREVIEW_CLOSE_DELAY_MS", 150)
    controller = panel._preview
    open_preview(panel, 0)
    popover = controller.popover()
    QApplication.sendEvent(popover, QEvent(QEvent.Enter))
    QApplication.sendEvent(popover, QEvent(QEvent.Leave))
    assert controller._closer.interval() == 150
    # Re-entering cancels it, so a jitter at its own edge while reading
    # does not close it.
    QApplication.sendEvent(popover, QEvent(QEvent.Enter))
    assert controller._closer.isActive() is False

    QApplication.sendEvent(popover, QEvent(QEvent.Leave))
    fire(controller._closer)
    assert controller.is_open() is False


def test_nothing_inside_the_popover_swallows_the_pointer(panel):
    # popovers.md asks to "limit the amount of functionality in the
    # popover to a few related tasks"; here the number of tasks is zero,
    # so every child hands its mouse events to the window.
    open_preview(panel, 0)
    popover = panel._preview.popover()
    children = popover.findChildren(QWidget)
    assert children
    for child in children:
        assert child.testAttribute(Qt.WA_TransparentForMouseEvents) is True


def test_moving_to_an_ineligible_row_closes_after_the_grace(panel):
    controller = panel._preview
    open_preview(panel, 0)
    panel._store_ref.set_failed(identifier_at(panel, 1), "signer timed out")
    assert controller.is_open() is True

    leave(panel, 0)
    move_onto(panel, 1)
    assert controller.is_armed() is False
    assert controller._closer.isActive() is True
    fire(controller._closer)
    assert controller.is_open() is False


# --------------------------------------------------------------------------- #
# The disarm rule                                                             #
# --------------------------------------------------------------------------- #

def test_scrolling_closes_and_disarms_and_the_next_move_re_arms(panel):
    controller = panel._preview
    open_preview(panel, 0)
    bar = panel._list.verticalScrollBar()
    bar.setRange(0, 200)
    bar.setValue(40)
    assert controller.is_open() is False
    assert controller.is_disarmed() is True
    # A disarmed controller opens nothing even if a timer fires late.
    controller._pending_id = "d1"
    controller._on_dwell()
    assert controller.is_open() is False
    # The next real move inside a row re-arms it.
    move_onto(panel, 1)
    assert controller.is_disarmed() is False
    assert controller.is_armed() is True


def test_a_mouse_press_closes_and_disarms_until_release(panel):
    # popovers.md: "a popover generally closes when people click or tap
    # outside its bounds".
    controller = panel._preview
    open_preview(panel, 0)
    press(panel, 0)
    assert controller.is_open() is False
    assert controller.is_disarmed() is True


def test_a_drag_across_rows_is_not_a_dwell(panel):
    controller = panel._preview
    QApplication.sendEvent(row_of(panel, 1), QMouseEvent(
        QEvent.MouseMove, QPointF(5, 5), QPointF(5, 5),
        Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
    ))
    assert controller.is_armed() is False


def test_a_wheel_event_on_a_row_closes_immediately(panel):
    controller = panel._preview
    open_preview(panel, 0)
    QApplication.sendEvent(row_of(panel, 0), QEvent(QEvent.Wheel))
    assert controller.is_open() is False
    assert controller.is_disarmed() is True


def test_a_search_keystroke_closes_and_leaves_no_widget_reference(panel):
    controller = panel._preview
    open_preview(panel, 0)
    rows_before = [row_of(panel, i) for i in range(panel._list.count())]

    panel._search_edit.setText("Article 1")
    assert controller.is_open() is False
    assert controller.is_disarmed() is True
    # The rebuild destroyed every row widget the preview could have been
    # holding; the controller holds identifiers and rects only.
    assert controller.shown_identifier() == ""
    assert controller.pending_identifier() == ""
    for attribute in vars(controller).values():
        assert attribute not in rows_before


def test_a_context_menu_closes_the_popover_before_it_is_built(panel):
    # popovers.md: "Don't show another view over a popover. Make sure
    # nothing displays on top of a popover, except for an alert."
    controller = panel._preview
    open_preview(panel, 0)
    seen = []
    # Returns no menu, so nothing opens a modal loop; the assertion is
    # about the state of the world at the moment the menu is built.
    panel._build_context_menu = lambda item: seen.append(controller.is_open())
    panel._on_context_menu(QPoint(10, 10))
    assert seen == [False]
    assert controller.is_disarmed() is True


def test_switching_to_feeds_closes_the_preview(panel):
    controller = panel._preview
    open_preview(panel, 0)
    panel._seg_feeds.setChecked(True)
    assert controller.is_open() is False
    assert panel.preview_is_available() is False


def test_a_hidden_panel_offers_no_preview(panel):
    controller = panel._preview
    open_preview(panel, 0)
    panel.hide()
    assert controller.is_open() is False
    assert panel.preview_is_available() is False
    # And a dwell that fires after the panel is gone opens nothing.
    assert controller.open_for("d0", QRect(0, 0, 320, 54)) is False


def test_hiding_a_docked_panel_takes_the_popover_with_it(panel, child_host):
    # The test above passes on the window deactivation a top-level panel
    # sends when it hides. In the application the panel is a child of the
    # splitter, nothing deactivates, and only ``hideEvent`` is left: with
    # the guard gone the popover is stranded over the editor.
    controller = panel._preview
    open_preview(panel, 0)
    assert controller.is_open() is True

    panel.hide()
    assert controller.is_open() is False
    assert controller.popover().isVisible() is False
    assert panel.preview_is_available() is False


def help_event(panel, index: int) -> bool:
    """Deliver the tooltip request Qt sends after its wake-up delay.

    Fusion asks for 700 ms, and only 20 ms when a tooltip was shown
    moments ago, so on a row the pointer is resting on this always
    arrives while the popover is up.
    """
    row = row_of(panel, index)
    pos = QPoint(5, 5)
    QApplication.sendEvent(row, QHelpEvent(QEvent.ToolTip, pos, row.mapToGlobal(pos)))
    return QToolTip.isVisible()


def test_no_tooltip_appears_beside_an_open_preview(panel, tooltips):
    # popovers.md: "Don't show another view over a popover. Make sure
    # nothing displays on top of a popover, except for an alert." The
    # popover deliberately does not cover the row, which is exactly why
    # the pointer stays on the row and the help event fires.
    open_preview(panel, 0)
    assert row_of(panel, 0).toolTip() != ""  # the row keeps its text
    assert help_event(panel, 0) is False
    # Nor on the row a pending swap is about to open, which the pointer
    # is equally resting on.
    leave(panel, 0)
    move_onto(panel, 1)
    assert panel._preview.pending_identifier() == identifier_at(panel, 1)
    assert help_event(panel, 1) is False


def test_a_row_that_gets_no_preview_keeps_its_tooltip(panel, tooltips):
    # The tooltip is the only place a FAILED row's full text exists, so
    # suppression stops where the preview stops.
    panel._store_ref.set_failed(identifier_at(panel, 1), "signer timed out")
    move_onto(panel, 1)
    assert panel._preview.is_armed() is False
    assert row_of(panel, 1).toolTip() != ""
    assert help_event(panel, 1) is True


def test_deactivating_the_window_closes_the_preview(panel):
    controller = panel._preview
    open_preview(panel, 0)
    QApplication.sendEvent(panel.window(), QEvent(QEvent.WindowDeactivate))
    assert controller.is_open() is False


# --------------------------------------------------------------------------- #
# Store changes under an open preview                                         #
# --------------------------------------------------------------------------- #

def test_a_removed_draft_takes_its_preview_with_it(panel):
    controller = panel._preview
    open_preview(panel, 0)
    panel._store_ref.remove(controller.shown_identifier())
    assert controller.is_open() is False


def test_a_profile_switch_closes_and_forgets_everything(panel):
    controller = panel._preview
    open_preview(panel, 0)
    controller._image_failures[HERO] = "network error"
    panel._store_ref.reset()
    assert controller.is_open() is False
    assert controller._image_failures == {}
    assert controller._digests == {}


def test_a_draft_that_stops_qualifying_closes_its_preview(panel):
    controller = panel._preview
    open_preview(panel, 0)
    identifier = controller.shown_identifier()
    panel._store_ref.set_failed(identifier, "signer timed out")
    assert controller.is_open() is False


def test_a_draft_that_finishes_decrypting_under_the_pointer_arms_itself(panel):
    store = panel._store_ref
    store.upsert_skeleton(wrap_meta("late", created_at=NOW))
    QApplication.processEvents()
    index = next(
        i for i in range(panel._list.count())
        if identifier_at(panel, i) == "late"
    )
    controller = panel._preview
    move_onto(panel, index)
    # LOADING, so nothing arms.
    assert controller.is_armed() is False

    store.set_decrypted("late", inner=article_inner(9))
    assert controller.is_armed() is True
    assert controller.pending_identifier() == "late"


def test_an_application_font_change_closes_the_preview(panel, app_font):
    controller = panel._preview
    open_preview(panel, 0)
    font = QFont(app_font.font())
    font.setPointSizeF(26)
    app_font.setFont(font)
    QApplication.processEvents()
    assert controller.is_open() is False


def test_a_theme_switch_restyles_in_place_rather_than_closing(panel):
    controller = panel._preview
    open_preview(panel, 0)
    popover = controller.popover()
    panel.apply_theme(False)
    assert controller.is_open() is True
    assert popover is controller.popover()
    assert THEME_TOKENS[False]["muted"] in popover.styleSheet()


# --------------------------------------------------------------------------- #
# Placement through the panel                                                 #
# --------------------------------------------------------------------------- #

def test_the_shown_popover_clears_the_row_it_describes(panel):
    controller = panel._preview
    open_preview(panel, 1)
    popover = controller.popover()
    card = popover.card_rect()
    anchor = controller.shown_anchor()
    assert not card.intersects(anchor)
    assert popover.side() in ("left", "right", "below", "above")


# --------------------------------------------------------------------------- #
# The hero image                                                              #
# --------------------------------------------------------------------------- #

def png_bytes(colour: str = "teal") -> bytes:
    image = QImage(64, 36, QImage.Format_RGB32)
    image.fill(QColor(colour))
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


@pytest.fixture
def loader(tmp_path):
    """A ThumbnailLoader with a fake transport and a throwaway cache."""
    nam = FakeNam()
    return ThumbnailLoader(cache_dir=tmp_path, nam=nam), nam


def test_a_panel_with_no_loader_reserves_no_box_and_builds_no_request(panel):
    # The default. A DraftsPanel built in a test cannot reach a network
    # unless the test deliberately injects a loader, and a box that
    # could never be filled is not reserved in the first place.
    controller = panel._preview
    assert controller._loader is None
    open_preview(panel, 0)
    popover = controller.popover()
    assert popover.fields().image_url != ""
    assert popover.reserved_image() is False
    assert popover.has_image() is False
    assert popover.image_note() == ""


def test_a_gate_refused_url_reserves_no_box_and_fires_no_request(panel, loader):
    thumbnails, nam = loader
    panel.set_preview_image_loader(thumbnails)
    store = panel._store_ref
    store.set_decrypted("d0", inner={
        "kind": 30023,
        "content": "body " * 300,
        "tags": [
            ["title", "Hostile hero"],
            ["summary", "A summary worth a window, several words long."],
            ["image", "http://127.0.0.1:8080/hero.png"],
        ],
    })
    index = next(
        i for i in range(panel._list.count())
        if identifier_at(panel, i) == "d0"
    )
    open_preview(panel, index)
    popover = panel._preview.popover()
    assert popover.reserved_image() is False
    assert nam.calls == []


def test_an_encrypted_draft_never_fetches_anything_on_hover(panel, loader):
    # A LOADING row has no tags to read, so there is nothing to fetch
    # and nothing that could be fetched; the request must not be made
    # speculatively either.
    thumbnails, nam = loader
    panel.set_preview_image_loader(thumbnails)
    store = panel._store_ref
    store.upsert_skeleton(wrap_meta("locked", created_at=NOW))
    QApplication.processEvents()
    index = index_of(panel, "locked")

    move_onto(panel, index)
    assert panel._preview.is_armed() is False
    assert panel._preview.open_for("locked", QRect(0, 0, 320, 54)) is False
    assert nam.calls == []


def test_a_slow_image_reserves_its_box_and_nothing_reflows(panel, loader, monkeypatch):
    monkeypatch.setattr(dp, "PREVIEW_IMAGE_SLOW_MS", 800)
    thumbnails, nam = loader
    data = png_bytes()
    nam._scripted.append(FakeReply(status=200, body=data, url=HERO))
    panel.set_preview_image_loader(thumbnails)

    open_preview(panel, 0)
    popover = panel._preview.popover()
    assert popover.reserved_image() is True
    before = popover.card_size()
    geometry_before = popover.geometry()
    assert len(nam.calls) == 1

    # Frame two: still waiting, and now saying so.
    fire(panel._preview._slow)
    assert popover.image_note() == "Loading image…"
    assert popover.card_size() == before

    # Frame three: the bytes land.
    nam.issued[0].finish()
    assert popover.has_image() is True
    assert popover.image_note() == ""
    assert popover.card_size() == before
    assert popover.geometry() == geometry_before


def test_a_failed_image_keeps_the_box_and_says_so(panel, loader):
    thumbnails, nam = loader
    from PySide6.QtNetwork import QNetworkReply

    nam._scripted.append(FakeReply(
        status=500, body=b"", url=HERO,
        error=QNetworkReply.UnknownContentError, error_string="HTTP 500",
    ))
    panel.set_preview_image_loader(thumbnails)
    open_preview(panel, 0)
    popover = panel._preview.popover()
    before = popover.card_size()

    nam.issued[0].finish()
    assert popover.reserved_image() is True
    assert popover.has_image() is False
    assert popover.image_note() == "Image unavailable"
    assert popover.card_size() == before


def test_a_broken_hero_is_not_requested_twice(panel, loader):
    thumbnails, nam = loader
    from PySide6.QtNetwork import QNetworkReply

    nam._scripted.append(FakeReply(
        status=404, body=b"", url=HERO,
        error=QNetworkReply.ContentNotFoundError, error_string="HTTP 404",
    ))
    panel.set_preview_image_loader(thumbnails)
    open_preview(panel, 0)
    nam.issued[0].finish()
    assert len(nam.calls) == 1

    panel._preview.close()
    leave(panel, 0)
    open_preview(panel, 0)
    # The per-session negative cache answered instead of the network.
    assert len(nam.calls) == 1
    assert panel._preview.popover().image_note() == "Image unavailable"


def test_a_slow_image_that_lands_after_a_swap_is_dropped(panel, loader):
    thumbnails, nam = loader
    nam._scripted.append(FakeReply(status=200, body=png_bytes(), url=HERO))
    panel.set_preview_image_loader(thumbnails)

    open_preview(panel, 0)
    leave(panel, 0)
    move_onto(panel, 1)
    fire(panel._preview._dwell)
    # The correlation was dropped at the swap, so the first draft's
    # image cannot paint itself into the second draft's window.
    nam.issued[0].finish()
    assert panel._preview.popover().has_image() is False


def test_the_loader_serves_a_repeat_url_from_the_content_cache(loader):
    thumbnails, nam = loader
    data = png_bytes()
    nam._scripted.append(FakeReply(status=200, body=data, url=HERO))
    ready, failed = [], []
    thumbnails.url_ready.connect(lambda *a: ready.append(a))
    thumbnails.url_failed.connect(lambda *a: failed.append(a))

    thumbnails.load_url(HERO)
    nam.issued[0].finish()
    assert failed == []
    assert ready[0][0] == HERO
    import hashlib
    assert ready[0][1] == hashlib.sha256(data).hexdigest()

    thumbnails.load_url(HERO)
    assert len(nam.calls) == 1      # no second request
    assert len(ready) == 2


def test_the_loader_refuses_an_unsafe_url_without_touching_the_network(loader):
    thumbnails, nam = loader
    failed = []
    thumbnails.url_failed.connect(lambda *a: failed.append(a))
    thumbnails.load_url("file:///etc/passwd")
    assert nam.calls == []
    assert failed[0][1] == "blob URL was not allowed"


def test_the_loader_refuses_a_redirect_into_loopback(loader):
    thumbnails, nam = loader
    nam._scripted.append(FakeReply(
        status=200, body=png_bytes(), url="http://127.0.0.1:9/x",
    ))
    failed = []
    thumbnails.url_failed.connect(lambda *a: failed.append(a))
    thumbnails.load_url(HERO)
    nam.issued[0].finish()
    assert failed[0] == (HERO, "blob URL was not allowed")


def test_the_loader_refuses_bytes_that_are_not_an_image(loader):
    thumbnails, nam = loader
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        b'<image xlink:href="/etc/passwd"/></svg>'
    )
    nam._scripted.append(FakeReply(status=200, body=svg, url=HERO))
    ready, failed = [], []
    thumbnails.url_ready.connect(lambda *a: ready.append(a))
    thumbnails.url_failed.connect(lambda *a: failed.append(a))
    thumbnails.load_url(HERO)
    nam.issued[0].finish()
    assert ready == []
    assert failed[0] == (HERO, "not an image")


@pytest.mark.parametrize("source", [(100, 400), (4000, 200), (16, 9)])
def test_the_hero_box_keeps_its_ratio_whatever_the_image_is(source):
    from PySide6.QtGui import QPixmap

    hero = dp._HeroImage(is_dark=True)
    hero.reset(width=360)
    box = hero.size()
    assert box.width() == 360
    assert abs(box.width() / box.height() - 16 / 9) < 0.02

    tall = QImage(*source, QImage.Format_RGB32)
    tall.fill(QColor("teal"))
    hero.set_pixmap(QPixmap.fromImage(tall))
    # Centre cropped into the fixed box, never letterboxed and never
    # stretched. layout.md: "don't change the aspect ratio of the
    # artwork; instead, scale it so that important visual content
    # remains visible."
    assert hero.size() == box
    assert hero.has_pixmap() is True
    assert not hero.grab().isNull()


# --------------------------------------------------------------------------- #
# Accessibility                                                               #
# --------------------------------------------------------------------------- #

def test_the_row_announces_everything_the_preview_shows():
    rec = article(expiration=NOW + 3 * 86_400, content="word " * 1240)
    spoken = _accessible_row_text(rec, now=NOW)
    # Words, not abbreviations, and no thousands separator: both are
    # read aloud badly. inclusion.md: "Avoid using specialized or
    # technical terms without defining them."
    assert "7 minutes read, 1240 words" in spoken
    assert "1,240" not in spoken
    assert "min read" not in spoken
    assert "Tagged python, qt" in spoken
    assert "Expires in 3 days" in spoken
    # The image says nothing: NIP-23 carries no alt text, and inventing
    # a description would be worse than silence.
    assert "image" not in spoken.lower()


def test_the_pinned_row_announcements_still_hold():
    # The two assertions tests/test_drafts_panel.py pins, re-checked
    # here because this change appends to the same string.
    rec = record(title="A title", snippet="Body words", content="", inner_tags=[
        ["source", "https://blog.example.com/x"],
    ])
    spoken = _accessible_row_text(rec, now=NOW)
    assert spoken.startswith("A title. Imported from blog.example.com. Saved ")
    assert spoken.endswith(".")
    assert "…" not in spoken


def test_a_loading_row_announces_no_preview_facts():
    spoken = _accessible_row_text(article(state=DraftState.LOADING), now=NOW)
    assert "read" not in spoken
    assert "Tagged" not in spoken


def test_space_on_the_focused_row_opens_the_preview_and_gives_it_focus(panel):
    from PySide6.QtGui import QKeyEvent

    controller = panel._preview
    panel._list.setCurrentRow(1)
    panel._list.setFocus(Qt.OtherFocusReason)
    QApplication.processEvents()

    event = QKeyEvent(QEvent.KeyPress, Qt.Key_Space, Qt.NoModifier, " ")
    assert QApplication.sendEvent(panel._list, event) is True
    assert event.isAccepted() is True
    assert controller.is_open() is True
    assert controller.opened_by_keyboard() is True

    popover = controller.popover()
    # Without focus the screen reader cursor never enters the window and
    # the keyboard route would be theatre.
    assert popover.focusPolicy() == Qt.StrongFocus
    assert popover.focusWidget() is not None


def test_space_again_closes_it_and_escape_returns_focus_to_the_list(panel):
    from PySide6.QtGui import QKeyEvent

    controller = panel._preview
    panel._list.setCurrentRow(1)
    controller.toggle_current_row()
    assert controller.is_open() is True
    current = panel._list.currentRow()

    controller.toggle_current_row()
    assert controller.is_open() is False

    controller.toggle_current_row()
    popover = controller.popover()
    QApplication.sendEvent(popover, QKeyEvent(
        QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier,
    ))
    assert controller.is_open() is False
    assert panel._list.hasFocus() is True
    # Escape leaves the current row exactly where it was.
    assert panel._list.currentRow() == current


def test_a_hover_preview_never_takes_focus_while_the_search_field_has_it(panel):
    controller = panel._preview
    panel._search_edit.setFocus(Qt.OtherFocusReason)
    QApplication.processEvents()
    open_preview(panel, 0)
    popover = controller.popover()
    assert controller.opened_by_keyboard() is False
    # Structurally impossible rather than conditionally avoided.
    assert popover.focusPolicy() == Qt.NoFocus
    assert bool(popover.windowFlags() & Qt.WindowDoesNotAcceptFocus) is True
    assert popover.testAttribute(Qt.WA_ShowWithoutActivating) is True
    assert panel._search_edit.hasFocus() is True


def commands(panel, index: int):
    menu = panel._build_context_menu(panel._list.item(index))
    return {
        action.text(): action.isEnabled()
        for action in menu.actions() if action.text()
    }, [action.text() for action in menu.actions() if action.text()]


def test_the_context_menu_carries_show_preview_only_where_it_would_work(panel):
    states, order = commands(panel, 0)
    # First, because the menu is how the Space key is discovered at all.
    assert order[0] == "Show Preview"
    assert states["Show Preview"] is True

    # A failed row has no preview, so the command is present and
    # disabled rather than present and silently doing nothing.
    panel._store_ref.set_failed(identifier_at(panel, 0), "signer timed out")
    states, _order = commands(panel, 0)
    assert states["Show Preview"] is False


def test_show_preview_names_its_shortcut_in_the_menu(panel):
    menu = panel._build_context_menu(panel._list.item(0))
    action = next(a for a in menu.actions() if a.text() == "Show Preview")
    assert action.shortcut().toString() == "Space"


def test_show_preview_describes_the_row_it_was_raised_on(panel):
    # A right-click does not move the selection, so the command must not
    # quietly preview whichever row happens to be current.
    panel._list.setCurrentRow(0)
    item = panel._list.item(2)
    panel._show_preview_for(item)
    assert panel._preview.shown_identifier() == item.data(Qt.UserRole)
    assert panel._list.currentRow() == 0


def test_every_line_of_the_popover_carries_its_own_accessible_name():
    popover = compose(article())
    assert popover.accessibleName() == "Draft preview"
    assert popover.accessibleDescription()
    fields = popover.fields()
    for name, label in popover.labels():
        if label.height():
            assert label.accessibleName() == getattr(fields, name)
    hero = popover.findChild(dp._HeroImage)
    assert hero.accessibleName() == "Article image"
    # No description, because NIP-23 carries no alt text to put in one.
    assert hero.accessibleDescription() == ""


# --------------------------------------------------------------------------- #
# Theme tokens                                                                #
# --------------------------------------------------------------------------- #

def hexes_in(css: str) -> set:
    return {m.upper() for m in re.findall(r"#[0-9A-Fa-f]{6}", css)}


@pytest.mark.parametrize("is_dark", [True, False])
def test_the_preview_sheet_invents_no_colour_of_its_own(is_dark):
    used = hexes_in(dp.preview_css(is_dark))
    defined = {v.upper() for v in THEME_TOKENS[is_dark].values()}
    assert used <= defined, f"literal hexes outside the token table: {used - defined}"


def test_both_preview_themes_come_from_one_template():
    strip = lambda css: re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    dark, light = strip(dp.preview_css(True)), strip(dp.preview_css(False))
    assert dark != light
    for css in (dark, light):
        # Qt treats px and pt in a stylesheet as absolute and discards
        # em, so a font-size here would freeze while the app scaled.
        assert "font-size" not in css

    def selectors(css):
        return sorted(
            line.strip()
            for line in css.splitlines()
            if line.rstrip().endswith("{") or line.rstrip().endswith(",")
        )

    assert selectors(dark) == selectors(light)


def relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


@pytest.mark.parametrize("is_dark", [True, False])
def test_every_preview_text_pair_clears_the_minimum_contrast(is_dark):
    # accessibility.md: up to 17 pt, all weights, 4.5:1, checked "in both
    # light and dark appearances".
    t = THEME_TOKENS[is_dark]
    pairs = [
        ("title on surface", t["row_fg"], t["chrome_bg"]),
        ("summary on surface", t["chrome_fg"], t["chrome_bg"]),
        ("metadata on surface", t["muted"], t["chrome_bg"]),
        ("expiry on surface", t["error_fg"], t["chrome_bg"]),
        ("image label on the box fill", t["chrome_fg"], t["border"]),
    ]
    for name, fg, bg in pairs:
        ratio = contrast(fg, bg)
        assert ratio >= 4.5, f"{name}: {fg} on {bg} is {ratio:.2f}:1"


def test_the_surface_paints_the_token_fill_and_not_a_literal():
    for is_dark in (True, False):
        popover = compose(article(), is_dark=is_dark)
        popover.place(dp.preview_geometry(
            QRect(1120, 400, 320, 54), popover.card_size(), SCREEN,
        ))
        image = popover.grab().toImage()
        halo = popover.halo()
        # Well inside the card, below the hero and clear of any glyph.
        sample = QColor(image.pixel(
            halo + popover.card_size().width() - 6,
            halo + popover.card_size().height() - 6,
        )).name().upper()
        assert sample == THEME_TOKENS[is_dark]["chrome_bg"].upper()


# --------------------------------------------------------------------------- #
# Lifetime                                                                    #
# --------------------------------------------------------------------------- #

def test_shutdown_leaves_no_timer_and_no_window_behind(panel):
    controller = panel._preview
    open_preview(panel, 0)
    popover = controller.popover()
    # Every timer armed, so shutting down has something to stop.
    controller._dwell.start(5_000)
    controller._closer.start(5_000)
    controller._slow.start(5_000)
    assert controller.has_active_timer() is True

    controller.shutdown()
    assert controller.has_active_timer() is False
    assert controller.popover() is None
    assert controller.is_open() is False
    assert popover.isVisible() is False


def test_shutdown_disconnects_the_image_loader(panel, loader):
    thumbnails, nam = loader
    panel.set_preview_image_loader(thumbnails)
    controller = panel._preview
    controller.shutdown()
    # A reply that settles after shutdown reaches nothing.
    thumbnails.url_failed.emit(HERO, "network error")
    assert controller._image_failures == {}
    assert controller._loader is None


def test_closing_the_panel_shuts_the_preview_down(panel):
    controller = panel._preview
    open_preview(panel, 0)
    panel.close()
    assert controller.popover() is None
    assert controller.has_active_timer() is False


def test_a_replaced_loader_is_disconnected_from_the_old_one(panel, tmp_path):
    first = ThumbnailLoader(cache_dir=tmp_path / "a", nam=FakeNam())
    second = ThumbnailLoader(cache_dir=tmp_path / "b", nam=FakeNam())
    controller = panel._preview
    panel.set_preview_image_loader(first)
    panel.set_preview_image_loader(second)
    controller._image_url = HERO
    first.url_failed.emit(HERO, "from the old loader")
    assert HERO not in controller._image_failures
    second.url_failed.emit(HERO, "from the new loader")
    assert controller._image_failures[HERO] == "from the new loader"
