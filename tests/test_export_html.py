"""Pins the handrolled HTML exporter and its load-time normalization.

The defect classes this file guards against:
- bullet nesting drift (ragged indents, depth jumps, 0-space bullets),
- marker formatting leaking into <li> content when "• " straddles
  fragments,
- round-trip damage: Qt re-parsing our own output must reproduce the
  document (the ONLY accepted change is 0-space bullets normalizing to
  4 spaces),
- images silently losing their bytes instead of embedding as data URIs,
- an image whose name is itself an address (a data: URI from a reopened
  export, a third-party URL) being erased instead of passed through,
- a document naming a file outside the caller's trusted roots and the
  exporter embedding it anyway.
"""

import base64
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import (
    QColor,
    QImage,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextImageFormat,
)

from export_html import (
    document_to_html,
    normalize_after_set_html,
    sniff_image_ext,
    sniff_image_mime,
)
from markdown_writer import READ_FEATURES, document_to_markdown


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


PLAIN = QTextCharFormat()


def _bold():
    fmt = QTextCharFormat()
    fmt.setFontWeight(700)
    return fmt


def _red():
    fmt = QTextCharFormat()
    fmt.setForeground(QColor("#E53935"))
    return fmt


def _doc(lines):
    """Build a document from (text, fmt) lines; fmt defaults to plain."""
    doc = QTextDocument()
    cur = QTextCursor(doc)
    first = True
    for entry in lines:
        if not first:
            cur.insertBlock()
        first = False
        if isinstance(entry, str):
            cur.insertText(entry, PLAIN)
        else:
            for text, fmt in entry:
                cur.insertText(text, fmt)
    return doc


# --------------------------------------------------------------------------- #
# Semantic structure
# --------------------------------------------------------------------------- #

def test_paragraphs_and_inline_formatting():
    doc = _doc([
        [("plain with ", PLAIN), ("bold", _bold()), (" and ", PLAIN),
         ("red", _red())],
    ])
    out = document_to_html(doc, "T")
    assert "<p>plain with <strong>bold</strong> and " in out
    assert '<span style="color:#e53935">red</span>' in out
    assert "<!doctype html>" in out
    assert "<title>T</title>" in out


def test_escaping():
    doc = _doc(['a < b & c > "d"'])
    out = document_to_html(doc, "<Esc> & Co")
    assert "a &lt; b &amp; c &gt; &quot;d&quot;" in out or \
           "a &lt; b &amp; c &gt; \"d\"" in out
    assert "<title>&lt;Esc&gt; &amp; Co</title>" in out


def test_a_link_is_written_only_to_the_web_an_email_address_or_the_page_itself():
    """A page links to http, https, mailto and relative addresses; any
    other scheme (javascript:, data:, file:, spelled as a browser would
    still read it) leaves its words as text."""
    import re as _re
    kept = ["https://example.org/a", "HTTP://example.org/b", "mailto:ich@example.org",
            "notes/next.html", "#part-2", "/about", "?page=2"]
    refused = ["javascript:alert(1)", " JavaScript:alert(2)", "java\tscript:alert(3)",
               "\x01javascript:alert(4)", "data:text/html,<b>5</b>", "file:///etc/passwd",
               "vbscript:msgbox(6)"]
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    for number, href in enumerate(kept + refused):
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(href)
        cursor.insertText(f"link{number}", fmt)
        cursor.insertText(" ", QTextCharFormat())
    out = document_to_html(doc, "Links")
    hrefs = _re.findall(r'<a href="([^"]*)">', out)
    import html as _html
    assert [_html.unescape(h) for h in hrefs] == kept
    for number in range(len(kept + refused)):
        assert f"link{number}" in out


def test_bullets_nest_by_indent():
    doc = _doc([
        "    • one",
        "        • two",
        "    • three",
    ])
    out = document_to_html(doc, "T")
    assert "<ul><li>one<ul><li>two</li></ul></li><li>three</li></ul>" in out


def test_zero_space_bullet_is_depth_one():
    doc = _doc(["• zero", "    • four"])
    out = document_to_html(doc, "T")
    # Both land at depth 1 in one list.
    assert "<ul><li>zero</li><li>four</li></ul>" in out


def test_ragged_depth_jump_opens_and_closes_levels():
    doc = _doc([
        "• a",
        "            • deep",   # 12 spaces = depth 3, jumping from 1
        "• b",
    ])
    out = document_to_html(doc, "T")
    assert ("<ul><li>a<ul><li><ul><li>deep</li></ul>"
            "</li></ul></li><li>b</li></ul>") in out


def test_marker_formatting_never_leaks():
    # The "    • " prefix carries bold, the content does not; slicing the
    # straddling fragment must drop the marker's formatting.
    doc = _doc([[("    • plain content", PLAIN)]])
    bold_marker = _doc([[("    • ", _bold()), ("content", PLAIN)]])
    out = document_to_html(bold_marker, "T")
    assert "<li>content</li>" in out
    assert "<strong>" not in out
    out2 = document_to_html(doc, "T")
    assert "<li>plain content</li>" in out2


def test_empty_paragraph_and_pre_wrap():
    doc = _doc(["a", "", "  indented", "b"])
    out = document_to_html(doc, "T")
    assert "<p>&nbsp;</p>" in out
    assert '<p style="white-space:pre-wrap">  indented</p>' in out


# --------------------------------------------------------------------------- #
# Images
# --------------------------------------------------------------------------- #

def _png_at(tmp_path, name="a" * 64):
    img = QImage(4, 4, QImage.Format_RGB32)
    img.fill(QColor("teal"))
    path = str(tmp_path / name)  # bare sha-style name, no extension
    img.save(path, "PNG")
    return path


def test_image_embeds_as_data_uri_with_provenance(tmp_path):
    path = _png_at(tmp_path)
    doc = QTextDocument()
    QTextCursor(doc).insertImage(path)
    out = document_to_html(doc, "T",
                           source_url_for=lambda p: "https://x.example/i.png",
                           image_roots=(str(tmp_path),))
    assert 'src="data:image/png;base64,' in out
    assert 'data-source-url="https://x.example/i.png"' in out
    # The payload must actually decode back to the file bytes.
    b64 = out.split("base64,")[1].split('"')[0]
    assert base64.b64decode(b64) == open(path, "rb").read()


def test_missing_image_falls_back_to_url_then_placeholder(tmp_path):
    gone = str(tmp_path / ("f" * 64))
    doc = QTextDocument()
    QTextCursor(doc).insertImage(gone)
    with_url = document_to_html(doc, "T",
                                source_url_for=lambda p: "https://x.example/i.png")
    assert 'src="https://x.example/i.png"' in with_url
    without = document_to_html(doc, "T")
    assert "[image unavailable]" in without


def test_image_outside_the_roots_is_never_read(tmp_path):
    # A document names the paths it embeds, and a hostile document can
    # name anything on the machine; only the roots the caller trusts
    # may be read.
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _png_at(outside, name="secret")
    root = tmp_path / "root"
    root.mkdir()
    doc = QTextDocument()
    QTextCursor(doc).insertImage(secret)
    out = document_to_html(doc, "T", image_roots=(str(root),))
    assert "[image unavailable]" in out
    assert 'src="data:' not in out
    payload = base64.b64encode(open(secret, "rb").read()).decode("ascii")
    assert payload not in out


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks only")
def test_symlink_out_of_the_root_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = _png_at(outside, name="secret")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "innocent"
    link.symlink_to(secret)
    doc = QTextDocument()
    QTextCursor(doc).insertImage(str(link))
    out = document_to_html(doc, "T", image_roots=(str(root),))
    assert "[image unavailable]" in out
    assert 'src="data:' not in out


def test_asset_resolver_supplies_bytes_alt_and_provenance(tmp_path):
    # An app-created image is named by a key no filesystem can resolve;
    # its bytes and its uploaded URL come from the resolver.
    data = open(_png_at(tmp_path), "rb").read()
    sha = "d" * 64
    key = f"myeditor-asset:{sha}"
    resolved = SimpleNamespace(
        data=data, mime="image/png", remote_url=f"https://x.example/{sha}",
        width=0, height=0, alt="a photo", sha256=sha,
    )
    fmt = QTextImageFormat()
    fmt.setName(key)
    fmt.setProperty(QTextImageFormat.ImageAltText, "a photo")
    doc = QTextDocument()
    QTextCursor(doc).insertImage(fmt)
    out = document_to_html(doc, "T", image_roots=(str(tmp_path),),
                           asset_resolver=lambda name: resolved)
    assert 'src="data:image/png;base64,' in out
    assert f'data-source-url="https://x.example/{sha}"' in out
    assert 'alt="a photo"' in out
    assert key not in out


def test_data_uri_named_image_is_reexported_unchanged(tmp_path):
    # Reopening an exported .html names every embedded image by its
    # whole data URI. Saving again used to answer "[image unavailable]"
    # and destroy the user's own picture on a routine second save.
    data = open(_png_at(tmp_path), "rb").read()
    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    doc = QTextDocument()
    doc.setHtml(f'<p><img src="{uri}"></p>')
    out = document_to_html(doc, "T", image_roots=(str(tmp_path),))
    assert "[image unavailable]" not in out
    assert f'src="{uri}"' in out


def test_foreign_urls_survive_export_byte_identical(tmp_path):
    # Media this app did not create is never rewritten and never
    # dropped, whichever server it came from.
    urls = [
        "https://other.example/pictures/cat.png",
        f"https://blossom.other.example/{'c' * 64}.png",
        "http://plain.example/legacy.gif",
    ]
    doc = QTextDocument()
    doc.setHtml("".join(f'<p><img src="{u}"></p>' for u in urls))
    out = document_to_html(doc, "T", image_roots=(str(tmp_path),))
    assert "[image unavailable]" not in out
    for url in urls:
        assert f'src="{url}"' in out


def test_a_url_with_a_query_string_reopens_identical():
    # The ampersand must be escaped to stay valid inside the attribute,
    # and the parser must give the original name back on reopening.
    url = "https://pics.example.org/photos/cat.png?size=large&v=2"
    doc = QTextDocument()
    doc.setMarkdown(f"![cat]({url})")
    out = document_to_html(doc, "T")
    assert 'src="https://pics.example.org/photos/cat.png?size=large&amp;v=2"' in out

    reopened = QTextDocument()
    reopened.setHtml(out)
    normalize_after_set_html(reopened)
    names = []
    block = reopened.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and frag.charFormat().isImageFormat():
                names.append(frag.charFormat().toImageFormat().name())
            it += 1
        block = block.next()
    assert names == [url]


def test_unresolved_asset_key_never_leaks_into_src(tmp_path):
    fmt = QTextImageFormat()
    fmt.setName("myeditor-asset:" + "e" * 64)
    doc = QTextDocument()
    QTextCursor(doc).insertImage(fmt)
    out = document_to_html(doc, "T", image_roots=(str(tmp_path),))
    assert "[image unavailable]" in out
    assert "myeditor-asset" not in out


def test_alt_text_is_emitted(tmp_path):
    path = _png_at(tmp_path)
    fmt = QTextImageFormat()
    fmt.setName(path)
    fmt.setProperty(QTextImageFormat.ImageAltText, 'a "quoted" cat')
    doc = QTextDocument()
    QTextCursor(doc).insertImage(fmt)
    out = document_to_html(doc, "T", image_roots=(str(tmp_path),))
    assert 'alt="a &quot;quoted&quot; cat"' in out


def test_explicit_size_is_emitted(tmp_path):
    path = _png_at(tmp_path)
    fmt = QTextImageFormat()
    fmt.setName(path)
    fmt.setWidth(120)
    fmt.setHeight(80)
    doc = QTextDocument()
    QTextCursor(doc).insertImage(fmt)
    out = document_to_html(doc, "T", image_roots=(str(tmp_path),))
    assert 'width="120"' in out
    assert 'height="80"' in out


def test_mime_and_ext_sniffing():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 8
    assert sniff_image_mime(png) == "image/png"
    assert sniff_image_mime(jpg) == "image/jpeg"
    assert sniff_image_ext(png) == ".png"
    assert sniff_image_ext(jpg) == ".jpg"
    assert sniff_image_mime(b"not an image") is None
    assert sniff_image_ext(b"not an image") is None


# --------------------------------------------------------------------------- #
# Round-trip: export -> setHtml -> normalize
# --------------------------------------------------------------------------- #

def _roundtrip(doc, title="T", image_roots=()):
    out = document_to_html(doc, title, image_roots=image_roots)
    doc2 = QTextDocument()
    doc2.setHtml(out)
    normalize_after_set_html(doc2)
    return doc2


def test_roundtrip_preserves_text_and_structure():
    doc = _doc([
        "Title line",
        [("mixed ", PLAIN), ("bold", _bold()), (" tail", PLAIN)],
        "",
        "  indented plain",
        "last",
    ])
    doc2 = _roundtrip(doc)
    assert doc2.toPlainText() == doc.toPlainText()


def test_roundtrip_turns_typed_bullets_into_real_lists():
    # Bullets typed as text are exported as a list, and a list opened
    # from HTML is a real one, which the editor edits as such.
    doc = _doc(["intro", "    • one", "        • two", "    • three", "after"])
    doc2 = _roundtrip(doc)
    assert document_to_markdown(doc2) == document_to_markdown(doc) == (
        "intro\n\n- one\n    - two\n- three\n\nafter\n")
    assert doc2.findBlockByNumber(1).textList() is not None


def test_roundtrip_preserves_inline_formats():
    doc = _doc([[("b", _bold()), ("r", _red())]])
    doc2 = _roundtrip(doc)
    block = doc2.begin()
    runs = []
    it = block.begin()
    while not it.atEnd():
        frag = it.fragment()
        runs.append((frag.text(), frag.charFormat()))
        it += 1
    joined = "".join(t for t, _f in runs)
    assert joined == "br"
    bold_run = next(f for t, f in runs if "b" in t)
    red_run = next(f for t, f in runs if "r" in t)
    assert bold_run.fontWeight() > 400
    assert red_run.foreground().color().name() == "#e53935"


def test_roundtrip_keeps_embedded_image(tmp_path):
    # After reopening, the image must still be an inline object (loaded
    # from the data URI, not from the original cache path).
    img = QImage(4, 4, QImage.Format_RGB32)
    img.fill(QColor("teal"))
    path = str(tmp_path / ("b" * 64))
    img.save(path, "PNG")
    doc = QTextDocument()
    cur = QTextCursor(doc)
    cur.insertText("x ", PLAIN)
    cur.insertImage(path)
    doc2 = _roundtrip(doc, image_roots=(str(tmp_path),))
    assert "\ufffc" in doc2.toPlainText()


def test_foreign_lists_stay_lists_without_stray_whitespace():
    # Foreign HTML with real lists (pretty-printed whitespace included)
    # opens as real lists, the items trimmed.
    doc = QTextDocument()
    doc.setHtml("<ul>\n  <li>alpha</li>\n  <li>beta\n    <ul><li>gamma</li></ul>\n  </li>\n</ul>")
    normalize_after_set_html(doc)
    assert document_to_markdown(doc) == "- alpha\n- beta\n    - gamma\n"


# --------------------------------------------------------------------------- #
# Markdown structure: real lists, headings, inline styles, links
# --------------------------------------------------------------------------- #

def _from_markdown(text):
    doc = QTextDocument()
    doc.setMarkdown(text, READ_FEATURES)
    return doc


def test_a_real_list_exports_as_nested_list_markup():
    out = document_to_html(_from_markdown("- one\n    - inner\n- two\n"), "T")
    assert "<ul><li>one<ul><li>inner</li></ul></li><li>two</li></ul>" in out


def test_a_numbered_list_keeps_its_start_and_a_checklist_its_marks():
    out = document_to_html(_from_markdown("3. three\n4. four\n\nx\n\n- [x] done\n- [ ] open\n"),
                           "T")
    assert '<ol start="3"><li>three</li><li>four</li></ol>' in out
    assert '<ul><li class="checked">done</li><li class="unchecked">open</li></ul>' in out


def test_lists_of_two_kinds_at_one_depth_are_two_lists():
    doc = _from_markdown("- a\n- b\n")
    cursor = QTextCursor(doc.lastBlock())
    from PySide6.QtGui import QTextListFormat
    cursor.createList(QTextListFormat.Style.ListDecimal)
    out = document_to_html(doc, "T")
    assert "<ul><li>a</li></ul>\n<ol><li>b</li></ol>" in out


def test_headings_inline_styles_and_links_export():
    md = "## Title\n\nA ~~gone~~ `code` [site](https://x.example/?a=1&b=2) end\n"
    out = document_to_html(_from_markdown(md), "T")
    assert "<h2>Title</h2>" in out
    assert "<s>gone</s>" in out and "<code>code</code>" in out
    assert '<a href="https://x.example/?a=1&amp;b=2">site</a>' in out
    assert "<strong>" not in out           # a heading is bold by itself


@pytest.mark.parametrize("md", [
    "- one\n    - inner\n- two\n",
    "1. first\n2. second\n",
    "- [x] done\n- [ ] open\n",
    "# Big\n\n## Smaller\n\nbody\n",
    "A ~~gone~~ [site](https://x.example) end\n",
    "Intro\n\n> A quote\n>\n> > deeper\n\nAfter\n",
    "Above\n\n---\n\nBelow\n",
    "> - quoted item\n\nend\n",
])
def test_markdown_structure_survives_an_html_round_trip(md):
    # Review F6 (early range): quotes and dividers were lost on save.
    assert document_to_markdown(_roundtrip(_from_markdown(md))) == md


# -- fonts and sizes a page set ------------------------------------------------

def _runs(doc):
    """(text, char format) of every run of text, pictures left out."""
    found = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid() and not frag.charFormat().isImageFormat():
                found.append((frag.text(), frag.charFormat()))
            it += 1
        block = block.next()
    return found


def _opened(page):
    doc = QTextDocument()
    doc.setHtml(page)
    normalize_after_set_html(doc)
    return doc


def _fixed(fmt):
    from PySide6.QtGui import QTextFormat
    return [prop for prop in (QTextFormat.Property.FontPixelSize,
                              QTextFormat.Property.FontPointSize,
                              QTextFormat.Property.FontFamilies,
                              QTextFormat.Property.FontFamily) if fmt.hasProperty(prop)]


def test_a_saved_document_reopens_in_the_writing_font_and_text_size():
    """Seen in the app: a document saved as HTML came back in monospace at
    14 px, the page's CSS for browsers, and the Font and Text Size settings
    no longer reached it. The writer keeps no fonts or sizes, so none are
    read back: the text follows the editor's font."""
    doc = QTextDocument()
    doc.setPlainText("Written here.")
    reopened = _opened(document_to_html(doc, "T"))
    assert [(text, _fixed(fmt)) for text, fmt in _runs(reopened)] == [("Written here.", [])]


def test_a_pages_sizes_go_and_headings_stay_larger():
    """A page's own sizes (a paragraph at 12 px beside a body at 16) would
    stay while the text size grows around them, against Apple's "maintain
    the relative hierarchy ... when people adjust text sizes". A heading
    keeps its size relative to the body."""
    from PySide6.QtGui import QTextFormat
    reopened = _opened("<html><body style='font-family:Georgia; font-size:16px'>"
                       "<p style='font-size:12px'>Small print.</p><h2>Heading</h2></body></html>")
    assert all(_fixed(fmt) == [] for _text, fmt in _runs(reopened))
    heading = _runs(reopened)[-1][1]
    assert heading.property(QTextFormat.Property.FontSizeAdjustment) == 2


def test_code_stays_code_through_saving_and_opening():
    """Words a page sets in a monospace font of their own are code; so is
    code this editor wrote, which used to come back as plain words. Text
    that is all in one monospace font is the page's font, not code."""
    import rich_text
    doc = QTextDocument()
    cursor = QTextCursor(doc)
    cursor.insertText("Run ")
    cursor.insertText("ls", rich_text.style_format(rich_text.CODE, True))
    cursor.insertText(" now.", QTextCharFormat())
    reopened = _opened(document_to_html(doc, "T"))
    assert [(text, fmt.fontFixedPitch()) for text, fmt in _runs(reopened)] == [
        ("Run ", False), ("ls", True), (" now.", False)]
    assert _runs(reopened)[1][1].fontFamilies() == rich_text.CODE_FAMILIES
    foreign = _opened("<html><body style='font-family:Helvetica'><p>Type "
                      "<code>make</code>.</p></body></html>")
    assert [(text, fmt.fontFixedPitch()) for text, fmt in _runs(foreign)] == [
        ("Type ", False), ("make", True), (".", False)]
    all_mono = _opened("<html><body style='font-family:Menlo, monospace'><p>Just words.</p>"
                       "</body></html>")
    assert not _runs(all_mono)[0][1].fontFixedPitch()

