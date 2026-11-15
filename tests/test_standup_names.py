# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Einreichungen names editors by their own profiles (M-4 item 7)."""

from __future__ import annotations

from nostr.ui.standup_names import ProfileNames
from tests.standup_vectors import pk


def test_an_editor_is_named_by_their_profile_read_once_and_known_people_first():
    reads, learned = [], []
    names = ProfileNames(known=lambda key: "Jana" if key == pk("writer") else "",
                         read=lambda key, answer: reads.append((key, answer)))
    names.learned.connect(lambda: learned.append(True))
    assert names.name(pk("writer")) == "Jana" and reads == []
    assert names.name(pk("editor")) == ""                     # asked for, once
    assert names.name(pk("editor")) == "" and len(reads) == 1
    reads[0][1]({"display_name": "  Anna\n Berger ", "name": "anna"})
    assert learned == [True] and names.name(pk("editor")) == "Anna Berger"
    names.name(pk("contributor"))
    reads[1][1](None)                                         # couldn't be read: nothing said
    assert names.name(pk("contributor")) == "" and len(reads) == 2 and learned == [True]
