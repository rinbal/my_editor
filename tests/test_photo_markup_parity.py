# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The export writes every picture as STANDUP's md.py writes it.

tests/fixtures/photo_markup_cases.json holds STANDUP's own picture cases
(its tests/fixtures/article_render.json): Markdown, the page language,
the pictures' sizes, and the HTML md.py writes. Each case the editor holds
as rich text is exported (with pictures of those sizes, as the export
carries them) and each group and picture alone must be md.py's, once
what the export does on purpose is read back: a picture is in the page
(a data: address beside the address it came from, not loaded lazily),
an address is written as it was typed (md.py percent-encodes it), and
each photo unit sits in a column of its own. A picture's address standing
bare is exported as the picture, as the window's exports do
(bare_pictures.show on their copy).
"""

from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage, QTextDocument
from PySide6.QtWidgets import QApplication

import bare_pictures
import i18n
from export_html import document_to_html
from markdown_writer import holds_faithfully, read_markdown

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "photo_markup_cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

# Cases the export writes its own way, and why.
OWN_WAY = {
    "unsafe picture addresses open nothing":
        "a picture the export cannot read is '[image unavailable]'; an SVG it reads is carried",
    "a picture written into the page shows by one rule of prefixes":
        "a data: picture of the document is written as it is, and opens when it is a raster",
}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _png(width: int, height: int) -> bytes:
    picture = QImage(width, height, QImage.Format.Format_RGB32)
    picture.fill(QColor("#888"))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, "PNG")
    return bytes(data)


def _pictures(case) -> dict:
    """A picture of each size the case gives (one no picture has, as
    [0, 0] or ["1800", "1200"], leaves its size unknown)."""
    return {url: _png(*size) for url, size in (case.get("media_dims") or {}).items()
            if isinstance(size, list) and len(size) == 2
            and all(isinstance(v, int) and 0 < v <= 8000 for v in size)}


def _encoded(match) -> str:
    value = html.escape(quote(html.unescape(match.group(2)), safe=":/?#@!$&()*+,;=%"), quote=True)
    return f'{match.group(1)}="{value}"'


def _ours(page: str) -> list:
    units = [m.group(1) for line in page.split("\n")
             for m in [re.fullmatch(r'<div class="article-content" lang="(?:de|en)">(.*)</div>', line)] if m]
    out = []
    for unit in units:
        unit = re.sub(r'<img src="data:[^"]*" (alt="[^"]*"(?: width="\d+" height="\d+")?) '
                      r'data-source-url="([^"]*)" decoding="async">',
                      r'<img src="\2" \1 loading="lazy" decoding="async">', unit)
        out.append(re.sub(r'(src|href)="(https?://[^"]*)"', _encoded, unit))
    return out


def _theirs(page: str) -> list:
    return [line.replace(" />", ">") for line in page.split("\n")
            if line.startswith(('<div class="e21-imagegroup', "<figure"))]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_picture_is_written_as_md_py_writes_it(app, case):
    if case["name"] in OWN_WAY:
        pytest.skip(OWN_WAY[case["name"]])
    if not holds_faithfully(case["markdown"]):
        pytest.skip("the editor opens this Markdown as text")
    doc = QTextDocument()
    read_markdown(doc, case["markdown"])
    bare_pictures.show(doc)
    i18n.install("de" if (case.get("lang") or "").startswith("de") else i18n.ENGLISH)
    try:
        page = document_to_html(doc, "x", remote_pictures=_pictures(case))
    finally:
        i18n.install(i18n.ENGLISH)
    assert _ours(page) == _theirs(case["html"])


def test_every_case_is_written_as_md_py_does_or_named():
    """A case the export writes its own way is named, and only while it is
    one of STANDUP's cases, so a case copied again is compared again."""
    names = {case["name"] for case in CASES}
    assert set(OWN_WAY) <= names
    compared = [n for n in names - set(OWN_WAY)
                if holds_faithfully(next(c for c in CASES if c["name"] == n)["markdown"])]
    assert len(compared) >= 15


@pytest.mark.skipif(not os.environ.get("STANDUP_REPO"),
                    reason="STANDUP_REPO names a STANDUP checkout to compare with")
def test_the_cases_are_standups():
    theirs = json.loads((Path(os.environ["STANDUP_REPO"]) / "tests" / "fixtures"
                         / "article_render.json").read_text(encoding="utf-8"))["cases"]
    pictures = [case for case in theirs
                if "e21-figure" in case["html"] or "e21-imagegroup" in case["html"]]
    assert CASES == pictures
