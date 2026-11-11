# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The modules kept free of what pyflakes finds: an unused import, an
undefined name, a variable never read. Skipped where pyflakes is not
installed (it is a development tool, not a dependency)."""

from __future__ import annotations

import glob
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLEAN = [
    "main_window.py",
    "nostr/failures.py",
    "nostr/bunker.py",
    "nostr/ui/connect_dialog.py",
    *glob.glob("nostr/standup/*.py", root_dir=ROOT),
    *glob.glob("nostr/ui/standup_*.py", root_dir=ROOT),
]


@pytest.mark.parametrize("path", sorted(CLEAN))
def test_pyflakes_finds_nothing(path):
    api = pytest.importorskip("pyflakes.api")
    reporter = pytest.importorskip("pyflakes.reporter")
    import io
    out = io.StringIO()
    with open(os.path.join(ROOT, path), encoding="utf-8") as source:
        found = api.check(source.read(), path, reporter.Reporter(out, out))
    assert found == 0, out.getvalue()
