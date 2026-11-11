# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The STANDUP modules stay readable (RG-M2r 11): at most 600 lines, or
their docstring says why they are longer; and what moved out of one is
still offered by it, as the same object."""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULES = sorted([*ROOT.glob("nostr/standup/*.py"), *ROOT.glob("nostr/ui/standup_*.py")])
MOST_LINES = 600


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(ROOT)))
def test_a_standup_module_is_short_or_says_why_not(path):
    source = path.read_text(encoding="utf-8")
    if source.count("\n") <= MOST_LINES:
        return
    docstring = ast.get_docstring(ast.parse(source)) or ""
    assert "longer than most" in docstring, f"{path.name}: over {MOST_LINES} lines, no reason"


@pytest.mark.parametrize("module, moved, names", [
    ("nostr.standup.submitting", "nostr.standup.progress",
     ["Failure", "HELD_UNKNOWN_FIRST", "HELD_WORDS", "Held", "HeldWords", "MessageStatus",
      "PHASE_OF", "Phase", "Progress", "Step"]),
    ("nostr.standup.submitting", "nostr.standup.ports", ["Ports"]),
    ("nostr.ui.standup_submit", "nostr.ui.standup_uploads",
     ["BlossomUploads", "THUMB_SIDE", "thumbnail"]),
    ("nostr.ui.standup_submit_sheet", "nostr.ui.standup_publications",
     ["publication_logo", "recent_first", "rules_words"]),
    ("nostr.ui.standup_submit", "nostr.ui.standup_run", ["SubmissionRun"]),
    ("nostr.ui.standup_handoff", "nostr.ui.standup_waits", ["wait_words"]),
    ("nostr.ui.standup_publish", "nostr.ui.standup_publish_job",
     ["HeldPublish", "SignedHere", "offers_here", "publish_job"]),
    ("nostr.ui.standup_publish", "nostr.ui.standup_publish_words",
     ["ADDRESS_TAKEN", "BUSY", "BUSY_DETAIL", "BUSY_SIGNING", "DONE", "LINK_RELAYS",
      "NOT_PUBLISHABLE", "NOT_PUBLISHED", "NOT_REPORTED", "NOT_THE_SAME", "PICTURE_NOT_FOUND",
      "PICTURE_NOT_UP", "SOMEONE_ELSES", "Stage"]),
    ("nostr.standup.following", "nostr.standup.situations",
     ["ACTION_LABELS", "Action", "NOTHING_TO_DO", "ON_A_DECISION", "ON_ITS_WAY", "Section",
      "Situation", "TABLE", "Words"]),
])
def test_what_moved_out_is_still_offered_where_it_was(module, moved, names):
    import importlib
    old, new = importlib.import_module(module), importlib.import_module(moved)
    for name in names:
        assert getattr(old, name) is getattr(new, name)
        assert name in old.__all__ and name in new.__all__
