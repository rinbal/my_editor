# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Make nostr/language_display_names.json: region and script names for the
language chooser in a German interface, from Unicode CLDR.

The shared language catalogue (nostr/language_catalog.json, STANDUP's) names
languages, not regions or scripts, and Qt names them in English only. This
reads the pinned cldr-json release (cldr-localenames-full, main/<locale>/
territories.json and scripts.json) and keeps the names of the regions and
scripts the catalogue knows (its validation lists). Run it again to move to
another CLDR release:

    python scripts/cldr_display_names.py [--from DIR]

``--from`` reads the two files of each locale from DIR/<locale>/ instead of
the network (the files as cldr-json ships them).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

CLDR_VERSION = "48.0.0"
LOCALES = ("de",)
URL = ("https://raw.githubusercontent.com/unicode-org/cldr-json/{version}/cldr-json/"
       "cldr-localenames-full/main/{locale}/{kind}.json")
ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "nostr" / "language_catalog.json"
OUT = ROOT / "nostr" / "language_display_names.json"


def _read(locale: str, kind: str, source: Path | None) -> dict:
    if source is not None:
        data = json.loads((source / locale / f"{kind}.json").read_text(encoding="utf-8"))
    else:
        url = URL.format(version=CLDR_VERSION, locale=locale, kind=kind)
        with urllib.request.urlopen(url, timeout=30) as answer:      # noqa: S310 (a fixed https URL)
            data = json.loads(answer.read().decode("utf-8"))
    return data["main"][locale]["localeDisplayNames"][kind]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="source", type=Path, default=None)
    args = parser.parse_args(argv)
    validation = json.loads(CATALOGUE.read_text(encoding="utf-8"))["validation"]
    regions = {code.upper() for code in validation["region"]}
    scripts = {code.title() for code in validation["script"]}
    locales = {}
    for locale in LOCALES:
        territories = _read(locale, "territories", args.source)
        names = _read(locale, "scripts", args.source)
        # The standard names (no "-alt-" forms), as Intl.DisplayNames gives them.
        locales[locale] = {
            "regions": {code: territories[code] for code in sorted(regions) if code in territories},
            "scripts": {code: names[code] for code in sorted(scripts) if code in names},
        }
    out = {
        "source": f"Unicode CLDR {CLDR_VERSION} (cldr-json, cldr-localenames-full: "
                  "territories.json, scripts.json)",
        "license": "Unicode License V3, Copyright © 2015-2024 Unicode, Inc. (the release's LICENSE), "
                   "https://www.unicode.org/license.txt",
        "generator": "scripts/cldr_display_names.py",
        "locales": locales,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=False) + "\n",
                   encoding="utf-8", newline="\n")
    for locale, tables in locales.items():
        print(f"{locale}: {len(tables['regions'])} regions, {len(tables['scripts'])} scripts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
