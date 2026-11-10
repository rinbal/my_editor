# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Many photo groups: reading and exporting take time in proportion to
their number, within a budget of processor time.

Processor time (time.process_time), not the clock: a busy machine does
not fail it, and a test of 10,000 groups that took 13 s of it before
(a layout made for a document that had none laid out every group) has
room to show a slip. Linear: four times the groups take at most eight
times the time (linear is four, quadratic sixteen), each the best of a
few runs so one slow moment does not decide it.
"""

from __future__ import annotations

import time

import pytest
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QApplication, QTextEdit

import export_html
import photo_group
from markdown_writer import read_markdown

SMALL, LARGE = 2500, 10000
READ_BUDGET = 8.0          # seconds of processor time for 10,000 groups
EXPORT_BUDGET = 6.0


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def _markdown(groups: int) -> str:
    return "".join(f"Paragraph {i}.\n\n![A {i}](a{i}.png)\n\n![B {i}](b{i}.png)\n\n"
                   for i in range(groups))


def _best(work, runs: int) -> float:
    kept = []                  # freed after timing, not inside it
    best = float("inf")
    for _ in range(runs):
        start = time.process_time()
        kept.append(work())
        best = min(best, time.process_time() - start)
    kept.clear()
    return best


def _read(text: str) -> QTextDocument:
    doc = QTextDocument()
    read_markdown(doc, text)
    return doc


def test_reading_10000_groups_is_linear_and_within_its_budget():
    small_text, large_text = _markdown(SMALL), _markdown(LARGE)
    assert len(photo_group.groups_in(_read(small_text))) == SMALL
    small = _best(lambda: _read(small_text), 3)
    large = _best(lambda: _read(large_text), 2)
    assert large < READ_BUDGET, f"{large:.2f} s"
    assert large < 8 * small, f"{SMALL}: {small:.2f} s, {LARGE}: {large:.2f} s"


def test_exporting_10000_groups_is_linear_and_within_its_budget():
    small_doc, large_doc = _read(_markdown(SMALL)), _read(_markdown(LARGE))
    assert export_html.document_to_html(small_doc, "x").count('role="group"') == SMALL
    small = _best(lambda: export_html.document_to_html(small_doc, "x"), 3)
    large = _best(lambda: export_html.document_to_html(large_doc, "x"), 2)
    assert large < EXPORT_BUDGET, f"{large:.2f} s"
    assert large < 8 * small, f"{SMALL}: {small:.2f} s, {LARGE}: {large:.2f} s"


def test_a_document_read_for_an_export_is_never_given_a_layout(monkeypatch):
    """Asking a document for its layout makes one, and from then on every
    change is laid out: reading 10,000 groups for an export or a check
    spent most of its time laying them out for nobody."""
    asked = []
    made = QTextDocument.documentLayout
    monkeypatch.setattr(QTextDocument, "documentLayout",
                        lambda doc: asked.append(doc) or made(doc))
    doc = _read(_markdown(20))
    export_html.document_to_html(doc, "x")
    assert asked == []


def test_folding_in_a_shown_editor_lays_each_picture_out_about_once(monkeypatch):
    """Reading the pictures outside the fold's edit block placed cursors
    that asked the layout where they stood: every paragraph about to be
    replaced was laid out first, eight or nine times a picture."""
    calls = []
    measure = photo_group._Handler.intrinsicSize
    monkeypatch.setattr(photo_group._Handler, "intrinsicSize",
                        lambda self, *args: calls.append(1) or measure(self, *args))
    view = QTextEdit()
    view.resize(900, 800)
    view.show()
    QApplication.processEvents()
    doc = view.document()
    photo_group.install(doc)
    groups = 300
    doc.setMarkdown(_markdown(groups))
    calls.clear()
    assert photo_group.fold(doc) == groups
    assert len(calls) <= 2 * 2 * groups, len(calls)
    view.close()
