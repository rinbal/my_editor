# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The photo group engine, vendored from STANDUP, against its vectors.

photo_layout.py is STANDUP's backend/htmlgen/photo_layout.py and
tests/fixtures/photo_layout_vectors.json is its
shared/photo-layout-vectors.json, byte for byte (the engine with an SPDX
header in front). Both apps then show every group the same way: the
same rows, the same stacks, the same runs on a phone, the same markup.

What is checked, as STANDUP's own check does: every case of the vectors
gives the same layout, templates, row geometry, markup nodes, HTML
skeleton and phone runs; the constants are the vectors' constants; no
run of similar pictures is ever split; and the two files are the ones
STANDUP ships (their hashes), so a change made here alone, or a newer
engine vendored without its vectors, fails.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import photo_layout
from photo_layout import (
    CONSTANTS,
    group_html,
    group_markup,
    layout,
    notation,
    phone_geometry,
    phone_segments,
    ratio_of,
    row_geometry,
    style_text,
)

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "photo_layout.py"
VECTORS = ROOT / "tests" / "fixtures" / "photo_layout_vectors.json"
SPDX = (b"# SPDX-FileCopyrightText: 2026 rinbal\n"
        b"# SPDX-License-Identifier: AGPL-3.0-or-later\n")

# The SHA-256 of STANDUP's files as vendored. Vendoring a newer engine
# means copying both files again and writing their new hashes here.
ENGINE_SHA256 = "1b36a84f33309fc52c4bef9ee58e6591500a468fb7691eb7155b33b292031853"
VECTORS_SHA256 = "6d1f21f193ebc89bb27bb5c323c195e4a0b48face4d215af6386ca7210ac368d"


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text(encoding="utf-8"))


def _engine_bytes() -> bytes:
    data = ENGINE.read_bytes()
    assert data.startswith(SPDX), "the vendored engine starts with its SPDX header"
    return data[len(SPDX):]


def test_the_engine_is_the_one_standup_ships():
    assert hashlib.sha256(_engine_bytes()).hexdigest() == ENGINE_SHA256


def test_the_vectors_are_the_ones_standup_ships():
    assert hashlib.sha256(VECTORS.read_bytes()).hexdigest() == VECTORS_SHA256


def test_the_engine_and_its_vectors_are_one_version(vectors):
    # Tuples and lists compare as JSON does (the vectors are JSON).
    assert json.loads(json.dumps(CONSTANTS)) == vectors["constants"]
    assert CONSTANTS["version"] == vectors["constants"]["version"]


@pytest.mark.skipif(not os.environ.get("STANDUP_REPO"),
                    reason="STANDUP_REPO names a STANDUP checkout to compare with")
def test_the_files_match_a_standup_checkout():
    """With STANDUP_REPO set (a checkout on this computer), the vendored
    files are its files exactly: the step to take before vendoring."""
    repo = Path(os.environ["STANDUP_REPO"])
    assert _engine_bytes() == (repo / "backend" / "htmlgen" / "photo_layout.py").read_bytes()
    assert VECTORS.read_bytes() == (repo / "shared" / "photo-layout-vectors.json").read_bytes()


def _skeleton(nodes, items) -> str:
    """The vectors' HTML: each figure reduced to its classes, its cell
    style and its index (as STANDUP's check writes it)."""
    def figure(index, properties):
        wide = " e21-figure--wide" if items[index].get("wide") is True else ""
        if properties is not None:
            return (f'<figure class="e21-figure{wide} e21-pg-cell" '
                    f'style="{style_text(properties)}">{index}</figure>')
        return f'<figure class="e21-figure{wide}">{index}</figure>'
    return group_html(nodes, figure)


def _computed(items) -> dict:
    result = layout(items)
    markup = group_markup(result, items)
    regular = CONSTANTS["regular"]
    rows = []
    for row in result:
        cap = CONSTANTS["wideCap"]["px"] if row["name"] == "wide" else regular["cap"]
        found = row_geometry(row["cells"], items, regular["width"], regular["gap"], cap)
        rows.append({"height": found["height"], "width": found["width"],
                     "tiles": [[t["index"], t["x"], t["y"], t["w"], t["h"]] for t in found["tiles"]]})
    phone = []
    for segment in phone_segments(result, items):
        phone.append({
            "figures": segment["figures"], "mode": segment["mode"], "cells": segment["cells"],
            "properties": segment["properties"],
            "geometry": [dict({"width": W, "viewport": V}, **phone_geometry(segment, items, W, V))
                         for W, V in ((350, 844), (280, 568))]})
    return {"layout": notation(result), "templates": [r["name"] for r in result],
            "rows": rows, "markup": markup, "html": _skeleton(markup, items), "phone": phone}


def test_every_case_gives_what_the_vectors_say(vectors):
    cases = vectors["cases"]
    assert len(cases) > 300
    wrong = []
    for case in cases:
        got = _computed(case["items"])
        want = {"layout": case["layout"], "templates": case["templates"],
                "rows": case["geometry"]["rows"], "markup": case["markup"],
                "html": case["html"], "phone": case["phone"]}
        if got != want:
            wrong.append(case["name"])
    assert wrong == []


def _flat(node) -> list:
    if node["kind"] == "run":
        return [i for cell in node["cells"] for i in _flat(cell)]
    return list(node["figures"]) if node["kind"] == "stack" else [node["index"]]


def test_no_run_of_similar_pictures_is_ever_split(vectors):
    """Pictures of a similar shape that follow each other are one run, and
    a run is one element of the markup (SPEC 16), in author order."""
    spread = CONSTANTS["phone"]["spread"]
    for case in vectors["cases"]:
        items = case["items"]
        # A group past the limit is not searched: every picture alone.
        over = len(items) > CONSTANTS["limits"]["maxGroup"]
        runs = []
        for i, item in enumerate(items):
            r = ratio_of(item)
            alone = (over or r >= CONSTANTS["panorama"] or r <= CONSTANTS["tall"]
                     or item.get("wide") is True)
            if runs and not alone and not runs[-1]["alone"]:
                shapes = [ratio_of(items[k]) for k in runs[-1]["figures"]] + [r]
                if max(shapes) <= min(shapes) * spread:
                    runs[-1]["figures"].append(i)
                    continue
            runs.append({"figures": [i], "alone": alone})
        nodes = group_markup(layout(items), items)
        assert [i for node in nodes for i in _flat(node)] == list(range(len(items))), case["name"]
        elements = [_flat(node) for node in nodes if node["kind"] == "run"]
        assert elements == [run["figures"] for run in runs if len(run["figures"]) > 1], case["name"]


def test_the_engine_reads_no_file_and_imports_nothing_of_the_app():
    """Vendored as it is: it needs nothing of MyEditor."""
    source = _engine_bytes().decode("utf-8")
    imports = [line.strip() for line in source.splitlines()
               if line.startswith(("import ", "from "))]
    assert imports == ["import math"]
    assert photo_layout.__doc__
