# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Comparing two versions word by word (nostr/standup/compare.py,
nostr/ui/standup_compare.py).

Pinned here: what is new and what was removed are marked by the word;
the work is bounded by size and by time, and past either bound nothing
is marked and both versions are still shown (lesson 13); a pair is
compared once, on the worker thread; the marks are an underline and a
strike, never colour alone; the one line of a version card."""

from __future__ import annotations

import threading

import pytest
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from nostr.standup import compare
from nostr.standup.compare import ADDED, REMOVED, SAME, Part, change_summary, compare_words
from nostr.ui.standup_compare import LEGEND, TOO_MANY, CompareView, Comparer

OLD = ("Der Anfang war leicht.\n\nEine Wallet auf dem Tablet, eine kurze Einweissung für "
       "das Team.\n\nEin Schild an der Tür reicht oft schon.")
NEW = ("Der Anfang war leicht.\n\nEine Wallet auf dem Tablet, eine kurze Einweisung für "
       "das Team.\n\nUnd die Buchhaltung? Lisa trägt jede Zahlung abends ein.\n\nEin Schild "
       "an der Tür reicht oft schon.")


@pytest.fixture(autouse=True)
def app():
    return QApplication.instance() or QApplication([])


def test_insertions_and_deletions_are_marked_by_the_word():
    result = compare_words(OLD, NEW)
    assert not result.too_many and result.changed
    marked = [(part.kind, part.text) for part in result.parts if part.kind != SAME]
    assert marked == [(REMOVED, "Einweissung"), (ADDED, "Einweisung"),
                      (ADDED, "\n\nUnd die Buchhaltung? Lisa trägt jede Zahlung abends ein.")]
    # Read in order, the new text is everything but what was removed.
    assert "".join(p.text for p in result.parts if p.kind != REMOVED) == NEW
    assert "".join(p.text for p in result.parts if p.kind != ADDED) == OLD


def test_the_same_text_has_no_changes():
    result = compare_words(OLD, OLD)
    assert not result.changed and result.parts == (Part(SAME, OLD),)


def test_past_the_size_bound_nothing_is_marked_and_both_versions_stay():
    for old, new in [
        # One changed stretch whose word table is past the bound.
        (" ".join(f"a{i}" for i in range(1100)), " ".join(f"b{i}" for i in range(1100))),
        # More words than are compared at all.
        ("wort " * (compare.MAX_WORDS // 2 + 10), "wort " * (compare.MAX_WORDS // 2 + 10) + "x"),
        # More paragraphs than are matched.
        ("\n\n".join(["a"] * (compare.MAX_PARAGRAPHS + 1)), "b"),
    ]:
        result = compare_words(old, new)
        assert result.too_many and result.parts == ()
        assert (result.old, result.new) == (old, new)


def test_past_the_time_bound_the_marking_stops():
    # The clock is read between the changed stretches: one that jumps
    # past the bound stops the work, whatever is left of it.
    ticks = iter([0.0] + [10.0] * 50)
    result = compare_words(OLD, NEW, clock=lambda: next(ticks), max_seconds=2.0)
    assert result.too_many


def test_the_work_grows_with_the_changes_not_with_the_square_of_the_text():
    # A long text with one changed paragraph compares that paragraph only.
    calls = []
    real = compare._words_apart
    try:
        compare._words_apart = lambda old, new, deadline: calls.append(
            (len(old.split()), len(new.split()))) or real(old, new, deadline)
        paragraphs = [f"Absatz {i} mit einigen Worten darin." for i in range(1500)]
        changed = list(paragraphs)
        changed[700] = "Absatz 700 mit anderen Worten darin."
        result = compare_words("\n\n".join(paragraphs), "\n\n".join(changed))
    finally:
        compare._words_apart = real
    assert calls == [(6, 6)] and not result.too_many


def test_the_one_line_of_a_version_card():
    assert change_summary(OLD, NEW) == "1 paragraph new, 1 changed"
    assert change_summary("Eins.\n\nZwei.", "Eins.\n\nZwei.") == "No changes to the text"
    assert change_summary("Eins kurz.", "Eins kurz. Und jetzt noch deutlich länger "
                          "geschrieben.") == "1 added to"
    assert change_summary("Eins.\n\nZwei.\n\nDrei.", "Eins.") == "2 removed"


def test_a_pair_is_compared_once_on_the_worker_thread():
    threads, calls = [], []

    def counted(old, new):
        threads.append(threading.current_thread())
        calls.append((old, new))
        return compare_words(old, new)

    comparer = Comparer(compare_words=counted)
    heard = []
    comparer.compare(OLD, NEW, heard.append)
    comparer.compare(OLD, NEW, heard.append)            # asked while it is computed
    QThreadPool.globalInstance().waitForDone()
    QApplication.processEvents()
    assert len(heard) == 2 and heard[0] is heard[1] and len(calls) == 1
    assert threads[0] is not threading.main_thread()
    comparer.compare(OLD, NEW, heard.append)            # asked again: answered at once
    assert len(heard) == 3 and len(calls) == 1 and comparer.computed == 1


def test_a_compare_stopped_with_its_account_is_heard_by_nobody():
    held = []
    comparer = Comparer(run=lambda fn, on_done, on_error: held.append((fn, on_done)))
    heard = []
    comparer.compare(OLD, NEW, heard.append)
    comparer.stop()                                     # sign-out, another account
    fn, on_done = held[0]
    on_done(fn())
    assert heard == [] and comparer.computed == 0


def inline(fn, on_done, on_error):
    on_done(fn())


def test_the_marks_are_an_underline_and_a_strike_never_colour_alone():
    view = CompareView(is_dark=False)
    view.show_pair(Comparer(run=inline), OLD, NEW)
    marked = [(kind, text) for kind, text in view.marked() if kind != SAME]
    assert marked == [(REMOVED, "Einweissung"), (ADDED, "Einweisung"),
                      (ADDED, "Und die Buchhaltung? Lisa trägt jede Zahlung abends ein.")]
    assert view.state.text() == LEGEND                  # and one line says what they mean


def test_too_many_changes_are_said_and_both_versions_are_shown():
    view = CompareView(is_dark=True)
    old, new = " ".join(f"a{i}" for i in range(1100)), " ".join(f"b{i}" for i in range(1100))
    view.show_pair(Comparer(run=inline), old, new)
    assert view.state.text() == TOO_MANY
    shown = view.text.toPlainText()
    assert old in shown and new in shown and "Before" in shown and "After" in shown
