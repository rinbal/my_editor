# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What this computer holds of the texts submitted (RG-M2r2a 1): a mark
per account and text, kept across restarts, only ever going from "an
entry recorded" to "the server stored it", and the lock's words for each
state (RG-M2r2a 2)."""

from __future__ import annotations

import json

import pytest

from nostr.standup import formats, words
from nostr.standup.link_names import MAX_FILE_BYTES, LinkNameLock, SubmittedMarks
from nostr.standup.progress import HELD_WORDS, Held

WRITER = "a1" * 32
OTHER = "b2" * 32


def test_a_mark_is_kept_across_restarts_per_account_and_text(tmp_path):
    path = tmp_path / "marks.json"
    SubmittedMarks(path).remember(WRITER, "ein-jahr", stored=False)
    again = SubmittedMarks(path)
    assert again.get(WRITER, "ein-jahr") is False
    assert again.get(WRITER.upper(), "ein-jahr") is False      # one account, any spelling
    assert again.get(OTHER, "ein-jahr") is None and again.get(WRITER, "anders") is None
    # No link name in the file: the work's coordinate hash.
    text = path.read_text(encoding="utf-8")
    assert "ein-jahr" not in text and formats.coordinate_hash(WRITER, "ein-jahr") in text


def test_a_mark_only_goes_from_recorded_to_stored(tmp_path):
    marks = SubmittedMarks(tmp_path / "marks.json")
    marks.remember(WRITER, "ein-jahr", stored=True)
    marks.remember(WRITER, "ein-jahr", stored=False)           # a later revision's entry
    assert SubmittedMarks(tmp_path / "marks.json").get(WRITER, "ein-jahr") is True


def test_a_mark_taken_back_is_gone(tmp_path):
    marks = SubmittedMarks(tmp_path / "marks.json")
    marks.remember(WRITER, "ein-jahr", stored=False)
    marks.forget(WRITER, "ein-jahr")
    assert SubmittedMarks(tmp_path / "marks.json").get(WRITER, "ein-jahr") is None


@pytest.mark.parametrize("writer, d", [("", "x"), ("zz" * 32, "x"), (WRITER, ""),
                                       (WRITER, None)])
def test_no_mark_without_an_account_and_a_link_name(tmp_path, writer, d):
    marks = SubmittedMarks(tmp_path / "marks.json")
    marks.remember(writer, d, stored=True)
    assert marks.get(writer, d) is None and not (tmp_path / "marks.json").exists()


def test_what_is_not_a_mark_is_not_read(tmp_path):
    path = tmp_path / "marks.json"
    good = formats.coordinate_hash(WRITER, "ein-jahr")
    path.write_text(json.dumps({"version": 1, "texts": {
        good: True, "not-a-key": True, formats.coordinate_hash(WRITER, "b"): "yes",
        formats.coordinate_hash(WRITER, "c"): 1}}), encoding="utf-8")
    marks = SubmittedMarks(path)
    assert marks.get(WRITER, "ein-jahr") is True
    assert marks.get(WRITER, "b") is None and marks.get(WRITER, "c") is None


def test_a_file_larger_than_the_bound_is_not_parsed_and_not_written_over(tmp_path):
    path = tmp_path / "marks.json"
    path.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    marks = SubmittedMarks(path)
    assert marks.get(WRITER, "ein-jahr") is None
    marks.remember(WRITER, "ein-jahr", stored=True)
    assert path.stat().st_size == MAX_FILE_BYTES + 1


def test_marks_of_a_writer_made_for_a_test_stay_in_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    marks = SubmittedMarks(kept=False)
    marks.remember(WRITER, "ein-jahr", stored=True)
    assert marks.get(WRITER, "ein-jahr") is True
    assert not any(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("lock, note, locked", [
    (LinkNameLock(), "", False),
    (LinkNameLock(checking=True), words.LINK_NAME_CHECKING, True),
    (LinkNameLock(unchecked=True), words.LINK_NAME_NOT_CHECKED, True),
] + [(LinkNameLock(held=held), HELD_WORDS[held].link_name, True) for held in Held],
    ids=["free", "checking", "unchecked"] + [f"held {held.value}" for held in Held])
def test_each_lock_says_its_own_words(lock, note, locked):
    assert lock.locked is locked and lock.words()[0] == note
    assert bool(lock.words()[1]) is locked
