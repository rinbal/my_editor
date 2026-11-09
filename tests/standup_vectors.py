# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared test vectors of STANDUP's editorial contract, loaded once
(tests/fixtures/standup_editorial_fixtures.json; tests/fixtures/README.md
names the source)."""

from __future__ import annotations

import json
import os

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                     "standup_editorial_fixtures.json")

with open(_PATH, encoding="utf-8") as _f:
    VECTORS = json.load(_f)

KEYS = VECTORS["keys"]
IDS = VECTORS["ids"]
NOW = VECTORS["now"]
SID = IDS["submission_id"]
ADDRESS = IDS["publication"]


def sk(name: str) -> bytes:
    """A fixture key's secret, as bytes."""
    return bytes.fromhex(KEYS[name]["sk"])


def pk(name: str) -> str:
    return KEYS[name]["pubkey"]
