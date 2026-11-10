# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo groups in the HTML and PDF exports.

The HTML export writes a group as STANDUP writes it (the vendored
engine's markup), carries its pictures, stylesheet and scripts in the
page, and is checked in a headless browser on this computer (no network)
at 1440 and 390 pixels: every picture shows all four coloured corners,
at its own shape, and at phone width each strip swipes, picture by
picture, without the page running sideways. The PDF export lays a group
out on the page and keeps a row on one page; both read no picture from
outside the document's folder, no file larger than the cap, and no
picture the decode policy refuses, and fetch web pictures only through
the guard.
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import weakref
from pathlib import Path

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTextDocument
from PySide6.QtWidgets import QApplication

import export_pictures
import photo_group
import photo_layout
from export_html import document_to_html
from markdown_writer import read_markdown
from tests.photo_corners import PHOTOS, SHAPES, shows_whole

DOC = ("# Bothy weekend\n\nWe walked in from the station.\n\n![Steg](portrait.png)\n\n"
       "*The jetty at dusk*\n\n![Loch](landscape.png)\n\n![Bay](photo.png)\n\n"
       "![Shore](photo.png)\n\n![Pier $& jetty](landscape.png)\n\n![Hut](square.png)\n\n"
       "![Panorama](panorama.png)\n\n![Plan](plan.png#wide)\n\n*Floor plan of the bothy*\n\n"
       "The next morning it rained.\n")
NAMES = ["portrait.png", "landscape.png", "photo.png", "photo.png", "landscape.png",
         "square.png", "panorama.png", "plan.png"]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _doc(markdown: str = DOC) -> QTextDocument:
    doc = QTextDocument()
    read_markdown(doc, markdown)
    return doc


def _page(markdown: str = DOC, **kwargs) -> str:
    return document_to_html(_doc(markdown), "Bothy", image_roots=[str(PHOTOS)], **kwargs)


# --------------------------------------------------------------------------- #
# The markup                                                                   #
# --------------------------------------------------------------------------- #

def test_a_group_is_written_as_the_engine_writes_it(app):
    page = _page()
    items = [photo_group.item(SHAPES[name], caption, name == "plan.png")
             for name, caption in zip(NAMES, ["The jetty at dusk"] + [""] * 6
                                      + ["Floor plan of the bothy"])]
    result = photo_layout.layout(items)
    match = re.search(r'<div class="e21-imagegroup-track">(.*?)</div></div></div>', page, re.S)
    assert match, page[:400]
    # The track reduced to its skeleton (each figure's classes, style and
    # index) is the engine's own (SPEC 3.5).
    figures = iter(range(len(items)))
    skeleton = re.sub(r'<figure class="([^"]*)" style="([^"]*)"[^>]*>.*?</figure>',
                      lambda m: f'<figure class="{m.group(1)}" style="{m.group(2)}">'
                                f'{next(figures)}</figure>', match.group(1), flags=re.S)

    def figure(index, properties):
        w, h = items[index]["w"], items[index]["h"]
        cls = ("e21-figure" + (" e21-figure--wide" if items[index]["wide"] else "")
               + (" e21-pg-cell" if properties is not None else ""))
        style = f"--ar:{w}/{h}" + (";" + photo_layout.style_text(properties) if properties else "")
        return f'<figure class="{cls}" style="{style}">{index}</figure>'
    assert skeleton == photo_layout.group_html(photo_layout.group_markup(result, items), figure)
    assert f'data-layout="{photo_layout.notation(result)}"' in page
    assert 'role="group" aria-label="8 photos"' in page


def test_captions_stay_in_their_figures_in_reading_order(app):
    page = _page()
    figures = re.findall(r"<figure .*?</figure>", page, re.S)
    assert len(figures) == 8
    assert "<figcaption>The jetty at dusk</figcaption>" in figures[0]
    assert "<figcaption>Floor plan of the bothy</figcaption>" in figures[-1]
    assert [bool("<figcaption>" in f) for f in figures] == [True] + [False] * 6 + [True]
    assert "e21-figure--wide" in figures[-1]


def test_the_page_carries_everything_it_shows(app):
    page = _page()
    assert page.count('src="data:image/png;base64,') == 8
    assert "<link" not in page and 'src="http' not in page and "@import" not in page
    assert ".e21-imagegroup" in page and 'id="e21-photo-words"' in page
    assert "function strips" in page and "Viewer" in page
    assert "loading=\"lazy\"" not in page


def test_a_page_speaks_the_apps_language(app):
    import i18n
    try:
        i18n.install("de")
        page = _page()
    finally:
        i18n.install(i18n.ENGLISH)
    assert 'aria-label="8 Fotos"' in page
    words = json.loads(re.search(r'<script type="application/json" id="e21-photo-words">(.*?)'
                                 r'</script>', page, re.S).group(1))
    assert words["photoOf"] == "Foto {k} von {n}" and words["viewer"] == "Vollansicht"
    assert words["enlargeNamed"] == "Vergrößern: {alt}"


def test_a_page_without_groups_has_no_group_parts(app):
    page = _page("![a](portrait.png)\n\nWords\n")
    assert "e21-imagegroup" not in page and "e21-photo-words" not in page


def test_the_words_of_a_caption_and_a_description_are_escaped(app):
    page = _page('![a & "c" \'d\'](photo.png)\n\n*1 \\< 2 & "q" \\<script\\>*\n\n'
                 "![b](square.png)\n")
    figures = re.findall(r"<figure .*?</figure>", page, re.S)
    assert len(figures) == 2
    assert 'alt="a &amp; &quot;c&quot; &#x27;d&#x27;"' in figures[0]
    assert "<figcaption>1 &lt; 2 &amp; &quot;q&quot; &lt;script&gt;</figcaption>" in figures[0]
    assert "<script>" not in figures[0]


def test_a_lone_wide_picture_is_carried(app):
    """#wide is how a picture is laid out, not where it is: a picture with
    it, alone, is read without it, as the editor shows it."""
    page = _page("Words\n\n![Plan](plan.png#wide)\n\nMore\n")
    assert page.count('src="data:image/png;base64,') == 1
    assert "e21-imagegroup" not in page


def test_the_plain_and_r_markdown_writers_write_the_paragraphs(app, monkeypatch):
    """What the publishing text and the .Rmd export write of a document
    with groups is what they wrote of it before there were groups."""
    from doc_walk import serialize_plain_with_images
    from rmarkdown import document_to_rmd
    grouped = _doc()
    assert photo_group.has_groups(grouped)
    monkeypatch.setattr(photo_group, "fold", lambda doc, *a, **k: 0)
    paragraphs = _doc()
    assert not photo_group.has_groups(paragraphs)

    def target(fmt):
        return f"[{fmt.name()} {fmt.property(fmt.Property.ImageAltText)}]"
    assert (serialize_plain_with_images(grouped, target)
            == serialize_plain_with_images(paragraphs, target))
    assert "[plan.png#wide Plan]\nFloor plan of the bothy" in serialize_plain_with_images(
        grouped, target)

    def body(doc):
        return document_to_rmd(doc, "Bothy", copy_image=lambda name: name).split("---", 2)[2]
    assert body(grouped) == body(paragraphs)
    assert body(grouped).count("![") == 8


# --------------------------------------------------------------------------- #
# Red team: what the export reads                                              #
# --------------------------------------------------------------------------- #

def _group_srcs(page: str):
    return re.findall(r'<figure [^>]*>(?:<img src="([^"]*)"|<em>)', page)


def test_pictures_outside_the_documents_folder_are_not_read(app, tmp_path):
    folder = tmp_path / "doc"
    folder.mkdir()
    secret = tmp_path / "secret.png"
    secret.write_bytes((PHOTOS / "photo.png").read_bytes())
    os.symlink(secret, folder / "link.png")
    (folder / "own.png").write_bytes((PHOTOS / "square.png").read_bytes())
    doc = _doc(f"![a](../secret.png)\n\n![b](link.png)\n\n![c]({secret})\n\n![d](own.png)\n")
    page = document_to_html(doc, "x", image_roots=[str(folder)])
    srcs = _group_srcs(page)
    assert len(srcs) == 4
    assert srcs[:3] == ["", "", ""]                 # not read: "[image unavailable]"
    assert srcs[3].startswith("data:image/png;base64,")


def test_a_decompression_bomb_is_not_carried(app, tmp_path):
    # A PNG header that claims 60,000 x 60,000 pixels.
    import struct
    import zlib
    header = struct.pack(">IIBBBBB", 60000, 60000, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + header
    bomb = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", len(header)) + chunk
            + struct.pack(">I", zlib.crc32(chunk)))
    (tmp_path / "bomb.png").write_bytes(bomb)
    (tmp_path / "own.png").write_bytes((PHOTOS / "square.png").read_bytes())
    page = document_to_html(_doc("![a](bomb.png)\n\n![b](own.png)\n"), "x",
                            image_roots=[str(tmp_path)])
    assert _group_srcs(page)[0] == ""


def _properties(page: str):
    return [part for style in re.findall(r'<figure [^>]*style="([^"]*)"', page)
            for part in style.split(";")]


def test_a_picture_of_a_shape_past_the_engines_limits_is_laid_out_at_3_2(app, tmp_path):
    """A shape the engine does not take (photo_layout.picture_size) gets
    its provisional 3:2, and the stylesheet shows it whole inside."""
    odd = QImage(3300, 100, QImage.Format.Format_RGB32)
    odd.fill(0xFF0090FF)
    assert odd.save(str(tmp_path / "odd.png"))
    (tmp_path / "photo.png").write_bytes((PHOTOS / "photo.png").read_bytes())
    page = document_to_html(_doc("![a](odd.png)\n\n![b](photo.png)\n"), "x",
                            image_roots=[str(tmp_path)])
    first = re.search(r"<figure [^>]*>", page).group(0)
    assert 'style="--ar:3/2;' in first and "data-provisional" in first
    assert page.count("data:image/png;base64,") == 2


def test_every_layout_property_is_a_bounded_number(app):
    number = re.compile(r"^--[a-z]+:\d{1,6}(\.\d{1,4})?(/\d{1,6})?$")
    properties = _properties(_page())
    assert properties and all(number.match(part) for part in properties), properties


def test_a_huge_file_is_never_read(app, tmp_path, monkeypatch):
    from image_safety import MAX_PICTURE_BYTES, ImageRootPolicy
    huge = tmp_path / "huge.png"
    with open(huge, "wb") as out:              # sparse: no disk space used
        out.write((PHOTOS / "square.png").read_bytes())
        out.truncate(MAX_PICTURE_BYTES + 1)
    read = []
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: read.append(self.name) or original(self))
    assert ImageRootPolicy([tmp_path]).read("huge.png", limit=MAX_PICTURE_BYTES) is None
    page = document_to_html(_doc("![a](huge.png)\n\n![b](huge.png#wide)\n"), "x",
                            image_roots=[str(tmp_path)])
    assert "huge.png" not in read and "data:image" not in page


class _FakeFetcher:
    """Answers from a table, at once or never; records what was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def fetch(self, url, *, on_success, on_failure):
        self.asked.append(url)
        answer = self.answers.get(url)
        if answer == "never":
            return
        if answer is None:
            on_failure("refused")
        else:
            on_success(answer, "image/png")


def test_web_pictures_are_carried_when_they_come_in_time(app):
    png = (PHOTOS / "photo.png").read_bytes()
    fetcher = _FakeFetcher({"https://e.org/a.png": png, "https://e.org/slow.png": "never"})
    got = export_pictures.gather(
        ["https://e.org/a.png", "https://e.org/held.png", "https://e.org/slow.png",
         "https://e.org/bad.png"],
        held=lambda url: png if url.endswith("held.png") else None,
        fetcher=fetcher, timeout_ms=50)
    assert set(got) == {"https://e.org/a.png", "https://e.org/held.png"}
    assert "https://e.org/held.png" not in fetcher.asked
    doc = _doc("![a](https://e.org/a.png)\n\n![b](https://e.org/slow.png#wide)\n")
    assert export_pictures.web_picture_names(doc) == ["https://e.org/a.png",
                                                      "https://e.org/slow.png"]
    page = document_to_html(doc, "x", remote_pictures=got)
    srcs = _group_srcs(page)
    assert srcs[0].startswith("data:image/png") and srcs[1] == "https://e.org/slow.png"


def test_a_web_picture_larger_than_the_cap_or_not_a_picture_is_dropped(app):
    fetcher = _FakeFetcher({"https://e.org/big.png": b"\x89PNG\r\n\x1a\n" + b"0" * (
        export_pictures.MAX_BYTES + 1), "https://e.org/page.png": b"<html>"})
    assert export_pictures.gather(["https://e.org/big.png", "https://e.org/page.png"],
                                  fetcher=fetcher, timeout_ms=50) == {}


def test_the_export_fetcher_never_asks_this_computer_or_its_network(app):
    """The real fetcher refuses a local or private address before any
    connection (nostr/imports/netguard.py)."""
    from nostr.imports.fetch import BlobFetcher

    class Refusing:
        def get(self, *_a, **_k):
            raise AssertionError("no request may be made")
    fetcher = BlobFetcher(max_bytes=export_pictures.MAX_BYTES, guard=Refusing())
    urls = ["http://127.0.0.1/a.png", "http://192.168.1.1/b.png", "http://localhost/c.png",
            "http://[::1]/d.png", "file:///etc/passwd", "http://nas.local/e.png"]
    assert export_pictures.gather(urls, fetcher=fetcher, timeout_ms=200) == {}


# --------------------------------------------------------------------------- #
# In a browser                                                                 #
# --------------------------------------------------------------------------- #

def _chrome():
    """A headless Chromium on this computer (Playwright's), or None."""
    found = os.environ.get("MYEDITOR_HEADLESS_CHROME")
    if found and os.path.exists(found):
        return found
    patterns = [
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",
        "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell",
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(os.path.expanduser(pattern)))
        if hits:
            return hits[-1]
    return None


CHROME = _chrome()
needs_chrome = pytest.mark.skipif(CHROME is None, reason="no headless Chromium on this computer")

# Added to a copy of the page for the test only: scroll every strip to its
# k-th picture (from #k=...), then write where each picture is, whether
# the page runs sideways, and the strips' pager into the page.
PROBE = """<script>
// The shot is taken by the browser's clock, not when a picture is ready:
// a picture is drawn only once it is decoded, so the probe has every
// picture decoded before it is drawn (it changes nothing in the layout).
document.querySelectorAll('img').forEach(img => { img.decoding = 'sync' })
window.addEventListener('load', () => setTimeout(() => {
  const k = Number((location.hash.match(/k=(\\d+)/) || [0, 0])[1])
  document.querySelectorAll('.e21-pg-run[data-phone="strip"]').forEach(seg => {
    const figures = seg.querySelectorAll('figure')
    const target = figures[Math.min(k, figures.length - 1)]
    seg.scrollLeft = target.offsetLeft - seg.offsetLeft
  })
  const boxes = [...document.querySelectorAll('.e21-imagegroup figure img')].map(img => {
    const r = img.getBoundingClientRect()
    return [r.left + scrollX, r.top + scrollY, r.width, r.height, img.naturalWidth, img.naturalHeight]
  })
  const out = document.createElement('pre')
  out.id = 'probe'
  out.textContent = JSON.stringify({boxes, scrollWidth: document.documentElement.scrollWidth,
    dots: document.querySelectorAll('.e21-pg-pager:not([hidden]) .e21-pg-dot').length})
  document.body.append(out)
}, 300))
</script>"""
EXTRA_CSS = "<style>.article-content .e21-figure img { object-fit: cover !important; aspect-ratio: 1 !important; }</style>"


def _run(args, tmp_path):
    flags = [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
             "--disable-background-networking", "--disable-component-update", "--disable-sync",
             "--no-default-browser-check", "--force-device-scale-factor=1",
             # The shot waits for every picture to be decoded and drawn.
             "--run-all-compositor-stages-before-draw",
             f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=4000"]
    return subprocess.run(flags + args, capture_output=True, text=True, timeout=120)


def _probed(page: str, width: int, tmp_path, probe: str, k: int = 0, flags=()):
    """The page at ``width`` with ``probe`` added, its path and address,
    and what the probe wrote."""
    path = tmp_path / f"page_{width}.html"
    path.write_text(page.replace("</body>", probe + "</body>"), encoding="utf-8")
    url = path.as_uri() + f"#k={k}"
    dom = _run(["--window-size=%d,3400" % width, *flags, "--dump-dom", url], tmp_path).stdout
    found = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S)
    assert found, dom[-2000:]
    return url, json.loads(html.unescape(found.group(1)))


def _look(page: str, width: int, tmp_path, k: int = 0, extra: str = ""):
    url, probe = _probed(page, width, tmp_path, extra + PROBE, k)
    shot = tmp_path / f"shot_{width}_{k}.png"
    _run([f"--window-size={width},3400", f"--screenshot={shot}", url], tmp_path)
    return probe, QImage(str(shot))


def _check_whole(probe, image, width, seen):
    for n, (x, y, w, h, nw, nh) in enumerate(probe["boxes"]):
        if x < -0.5 or x + w > width + 0.5:
            continue                                    # off to the side in a strip
        seen.add(n)
        assert nw and nh, n
        assert w / h == pytest.approx(nw / nh, rel=0.01), n
        assert shows_whole(image, QRectF(x, y, w, h)), n


@needs_chrome
def test_in_a_browser_at_1440_every_picture_is_whole(app, tmp_path):
    probe, image = _look(_page(), 1440, tmp_path)
    seen = set()
    _check_whole(probe, image, 1440, seen)
    assert seen == set(range(8))
    assert probe["scrollWidth"] == 1440 and probe["dots"] == 0
    for _x, _y, _w, h, _nw, _nh in probe["boxes"][:6]:
        assert h <= 560 + 0.5


@needs_chrome
def test_in_a_browser_at_390_strips_swipe_and_every_picture_is_whole(app, tmp_path):
    page = _page()
    seen = set()
    probe = None
    for k in range(4):                     # the strip holds four pictures
        probe, image = _look(page, 390, tmp_path, k)
        _check_whole(probe, image, 390, seen)
        assert probe["scrollWidth"] == 390
    assert seen == set(range(8))
    assert probe["dots"] == 4


# Added to a copy of the page for the test only: a reader at a phone with
# the keyboard alone (Reduce Motion on, so nothing waits on an animation).
READER = """<script>
document.querySelectorAll('img').forEach(img => { img.decoding = 'sync' })
// A headless browser on its own clock draws no frames, so a frame's work
// runs on a timer here, and the scroll event a frame sends is sent by hand.
window.requestAnimationFrame = work => setTimeout(() => work(performance.now()), 16)
const wait = ms => new Promise(done => setTimeout(done, ms))
const key = (el, name, more = {}) => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...more }))
  document.querySelectorAll('.e21-pg-run').forEach(seg => seg.dispatchEvent(new Event('scroll')))
}
const dots = seg => [...seg.nextElementSibling.querySelectorAll('.e21-pg-dot')]
  .map(dot => dot.getAttribute('aria-current') === 'true')
window.addEventListener('load', () => setTimeout(async () => {
  const out = {}
  const pictures = [...document.querySelectorAll('.e21-imagegroup figure img')]
  out.pictures = pictures.map(p => [p.getAttribute('role'), p.tabIndex, p.getAttribute('aria-label')])
  out.groups = [...document.querySelectorAll('.e21-imagegroup')]
    .map(g => [g.getAttribute('role'), g.getAttribute('aria-label')])
  const seg = document.querySelector('.e21-pg-run[data-phone="strip"]')
  out.strip = [seg.getAttribute('role'), seg.getAttribute('aria-label'), seg.tabIndex]
  out.dotNames = [...seg.nextElementSibling.querySelectorAll('.e21-pg-dot')].map(d => d.getAttribute('aria-label'))
  seg.focus()
  key(seg, 'ArrowRight'); await wait(300)
  out.right = dots(seg)
  out.spoken = seg.nextElementSibling.querySelector('.e21-pg-sr').textContent
  key(seg, 'End'); await wait(300)
  out.end = dots(seg)
  key(seg, 'Home'); await wait(300)
  out.home = dots(seg)

  const before = history.length
  const picture = pictures[2]
  picture.focus(); key(picture, 'Enter'); await wait(300)
  const viewer = document.querySelector('.pv')
  out.open = !viewer.hidden
  out.viewer = [viewer.getAttribute('role'), viewer.getAttribute('aria-modal'), viewer.getAttribute('aria-label')]
  out.count = viewer.querySelector('.pv-count').textContent
  out.shown = viewer.querySelector('.pv-img').src === picture.src
  out.focusInside = viewer.contains(document.activeElement)
  out.restInert = [...document.body.children].filter(c => c !== viewer).every(c => c.inert)
  out.locked = document.documentElement.style.overflow
  const stops = new Set()
  out.trapped = true
  for (let i = 0; i < 9; i++) {
    key(document.activeElement, 'Tab', { shiftKey: i > 4 })
    stops.add(document.activeElement.getAttribute('aria-label'))
    out.trapped = out.trapped && viewer.contains(document.activeElement)
  }
  out.stops = [...stops].sort()
  key(document.activeElement, 'ArrowRight'); await wait(50)
  out.countNext = viewer.querySelector('.pv-count').textContent
  key(document.activeElement, 'Escape'); await wait(400)
  out.closed = viewer.hidden
  out.focusBack = document.activeElement === pictures[3]
  out.unlocked = document.documentElement.style.overflow
  out.restBack = [...document.body.children].every(c => !c.inert)
  out.history = history.length - before
  const pre = document.createElement('pre')
  pre.id = 'probe'
  pre.textContent = JSON.stringify(out)
  document.body.append(pre)
}, 300))
</script>"""


@needs_chrome
def test_in_a_browser_a_reader_with_the_keyboard_swipes_and_opens_a_picture(app, tmp_path):
    _url, out = _probed(_page(), 390, tmp_path, READER, flags=["--force-prefers-reduced-motion"])
    # Every picture is a button with its name; the group says how many.
    assert out["groups"] == [["group", "8 photos"]]
    assert [role for role, _t, _l in out["pictures"]] == ["button"] * 8
    assert all(tab == 0 for _r, tab, _l in out["pictures"])
    assert out["pictures"][0][2] == "Enlarge: Steg"
    assert out["pictures"][4][2] == "Enlarge: Pier $& jetty"
    # The strip: a named stop, a dot per picture, arrows and Home and End.
    assert out["strip"] == ["group", "4 photos to swipe through", 0]
    assert out["dotNames"] == [f"Photo {k} of 4" for k in range(1, 5)]
    assert out["right"] == [False, True, False, False]
    assert out["spoken"] == "Photo 2 of 4"
    assert out["end"] == [False, False, False, True]
    assert out["home"] == [True, False, False, False]
    # The viewer: a modal dialog with its name, focus kept inside, the
    # page behind out of reach and still; Escape gives it all back, with
    # focus on the picture now shown, and no entry in the history.
    assert out["open"] and out["shown"]
    assert out["viewer"] == ["dialog", "true", "Full View"]
    assert out["count"] == "3 of 8"
    assert out["focusInside"] and out["trapped"]
    assert out["stops"] == sorted(["Close", "Open Original in a New Tab", "Previous Photo",
                                   "Next Photo"])
    assert out["restInert"] and out["locked"] == "hidden"
    assert out["countNext"] == "4 of 8"
    assert out["closed"] and out["focusBack"]
    assert out["unlocked"] == "" and out["restBack"] and out["history"] == 0


@needs_chrome
@pytest.mark.parametrize("width", [1440, 390])
def test_in_a_browser_long_and_right_to_left_captions_stay_in_the_page(app, tmp_path, width):
    words = ("![a](photo.png)\n\n*" + "Unbreakable" * 40 + "*\n\n![b](square.png)\n\n"
             "*\u05e9\u05dc\u05d5\u05dd \u200b\u200f\u202e reversed? \u2066isolated*\n\n"
             "![c](portrait.png)\n")
    page = _page(words)
    assert page.count("<figcaption>") == 2
    probe, image = _look(page, width, tmp_path)
    assert probe["scrollWidth"] == width
    seen = set()
    _check_whole(probe, image, width, seen)
    assert seen


@needs_chrome
def test_in_a_browser_a_cropped_picture_fails_the_check(app, tmp_path):
    """The negative control: object-fit: cover crops, and the check bites."""
    probe, image = _look(_page(), 1440, tmp_path, extra=EXTRA_CSS)
    cropped = [n for n, (x, y, w, h, _nw, _nh) in enumerate(probe["boxes"])
               if not shows_whole(image, QRectF(x, y, w, h))]
    # Every picture but the square one loses a corner.
    assert cropped == [n for n, name in enumerate(NAMES) if name != "square.png"]


# --------------------------------------------------------------------------- #
# PDF                                                                          #
# --------------------------------------------------------------------------- #

def _paged(markdown: str = DOC, page_setup=None):
    from PySide6.QtGui import QPdfWriter
    from export_pdf import RESOLUTION, default_page_setup, make_page_layout, paginate
    writer = QPdfWriter(os.devnull)
    writer.setResolution(RESOLUTION)
    writer.setPageLayout(make_page_layout(page_setup or default_page_setup()))
    return paginate(_doc(markdown), writer, RESOLUTION, image_roots=[str(PHOTOS)]), writer


def test_the_pdf_lays_a_group_out_on_the_page(app):
    paged, _writer = _paged()
    boxes = photo_group.picture_boxes(paged.clone)
    assert [photo_group.without_wide(name) for _p, name, _b in boxes] == NAMES
    for _p, name, box in boxes:
        w, h = SHAPES[photo_group.without_wide(name)]
        assert box.width() / box.height() == pytest.approx(w / h, rel=0.01)
        assert box.right() <= paged.page_width + 0.5
        # As on a reader's screen the size of the page: never taller than
        # its share of it.
        assert box.height() <= 0.85 * paged.content_height + 0.5


def test_a_pdf_keeps_a_row_on_one_page(app):
    filler = "\n\n".join(f"Paragraph {n} of the walk in." for n in range(34))
    paged, _writer = _paged(filler + "\n\n" + DOC)
    layout = paged.clone.documentLayout()
    rows = [frame for frame in paged.clone.rootFrame().childFrames()
            if photo_group.is_group_table(frame)]
    moved = 0
    for frame in rows:
        box = layout.frameBoundingRect(frame)
        assert int(box.top() // paged.content_height) == int((box.bottom() - 0.5)
                                                            // paged.content_height)
        moved += frame.format().pageBreakPolicy() != 0
    assert moved >= 1                       # the filler puts a row across a page end


def _shows_every_picture_whole(path, paged, resolution, page_layout):
    """Each group picture of ``paged`` on its page of the PDF at ``path``
    shows its four corners."""
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument
    pdf = QPdfDocument()
    assert pdf.load(str(path)) == QPdfDocument.Error.None_
    scale = 150 / resolution
    # The page's printable area starts at its margins.
    area = page_layout.paintRectPixels(resolution)
    pages = {}
    seen = []
    for _p, name, box in photo_group.picture_boxes(paged.clone):
        page = int(box.top() // paged.content_height)
        if page not in pages:
            size = pdf.pagePointSize(page)
            pages[page] = pdf.render(page, QSize(int(size.width() * 150 / 72),
                                                 int(size.height() * 150 / 72)))
        top = box.top() - page * paged.content_height
        on_page = QRectF((box.left() + area.left()) * scale, (top + area.top()) * scale,
                         box.width() * scale, box.height() * scale)
        assert shows_whole(pages[page], on_page), name
        seen.append(photo_group.without_wide(name))
    pdf.close()
    assert seen == NAMES


def test_the_pdf_shows_every_picture_whole(app, tmp_path):
    from export_pdf import RESOLUTION, export_pdf
    path = tmp_path / "bothy.pdf"
    export_pdf(_doc(), str(path), "Bothy", image_roots=[str(PHOTOS)])
    paged, writer = _paged()
    _shows_every_picture_whole(path, paged, RESOLUTION, writer.pageLayout())


def test_a_printed_page_shows_every_picture_whole(app, tmp_path):
    """File > Print lays a note out as the PDF export does (printing.py)."""
    from PySide6.QtPrintSupport import QPrinter
    from export_pdf import default_page_setup, make_page_layout, paginate
    from printing import print_document
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(str(tmp_path / "printed.pdf"))
    printer.setPageLayout(make_page_layout(default_page_setup()))
    assert print_document(_doc(), printer, image_roots=[str(PHOTOS)]) >= 1
    paged = paginate(_doc(), printer, printer.resolution(), image_roots=[str(PHOTOS)])
    _shows_every_picture_whole(tmp_path / "printed.pdf", paged, printer.resolution(),
                               printer.pageLayout())


def test_a_pdf_export_leaves_no_copy_of_the_document_behind(app):
    """Every export laid out a clone() that lived as long as the document,
    with all its pictures: the copy now goes with the export."""
    doc = _doc()
    from PySide6.QtGui import QPdfWriter
    from export_pdf import RESOLUTION, default_page_setup, make_page_layout, paginate
    writer = QPdfWriter(os.devnull)
    writer.setResolution(RESOLUTION)
    writer.setPageLayout(make_page_layout(default_page_setup()))
    paged = paginate(doc, writer, RESOLUTION, image_roots=[str(PHOTOS)])
    gone = weakref.ref(paged.clone)
    del paged
    import gc
    gc.collect()
    assert gone() is None
    assert doc.children() == [] or all(not isinstance(c, QTextDocument) for c in doc.children())
