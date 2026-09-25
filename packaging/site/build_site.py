#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build the install guide (site/) for GitHub Pages.

Copies site/ into the output folder, adds the app icon, and writes
install/downloads.json describing the latest release:

    {
      "version": "3.2",
      "page": "https://github.com/rinbal/my_editor/releases/tag/v3.2",
      "files": {
        "mac-arm64": {"name": "...dmg", "url": "...", "size": 48976225, "sha256": "..."},
        ...
      }
    }

File keys come from release_assets.asset_key, the same rule the in-app
updater uses. .github/workflows/install-guide.yml runs this on every deploy.
To preview locally:

    python packaging/site/build_site.py --out _site             # latest release
    python packaging/site/build_site.py --out _site --offline   # no network
    python -m http.server --directory _site 8000                # then open
                                                                # http://localhost:8000/install/

Plain stdlib, so CI needs no dependencies.
"""

import argparse
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from release_assets import asset_key  # noqa: E402

SITE = ROOT / "site"
ICON = ROOT / "packaging" / "icons" / "icon-256.png"
TRUSTED_PREFIX = "https://github.com/"


def repo_slug() -> str:
    """APP_REPO_SLUG from constants.py, read as text (constants imports Qt)."""
    text = (ROOT / "constants.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_REPO_SLUG\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("error: APP_REPO_SLUG not found in constants.py")
    return match.group(1)


def build_downloads(release: dict) -> dict:
    """downloads.json content for a GitHub API release object."""
    files = {}
    for asset in release.get("assets", []):
        name = asset.get("name") or ""
        url = asset.get("browser_download_url") or ""
        key = asset_key(name)
        if key is None or not url.startswith(TRUSTED_PREFIX):
            continue
        digest = asset.get("digest") or ""
        files[key] = {
            "name": name,
            "url": url,
            "size": int(asset.get("size") or 0),
            "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
        }
    tag = release.get("tag_name") or ""
    return {
        "version": tag[1:] if tag.startswith("v") else tag,
        "page": release.get("html_url") or "",
        "files": files,
    }


def fetch_latest_release(repo: str, token: str = None):
    """The latest release from the GitHub API, or None if there is none yet."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "my-editor-install-guide"},
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def build_site(out: Path, release) -> None:
    """Write the deployable site into ``out``, replacing what is there."""
    out = out.resolve()
    if out in (ROOT, SITE) or ROOT.is_relative_to(out) or out.is_relative_to(SITE):
        raise SystemExit(f"error: refusing to build into {out}")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE, out)
    shutil.copy2(ICON, out / "install" / "icon.png")
    downloads = build_downloads(release) if release else {"version": "", "page": "", "files": {}}
    (out / "install" / "downloads.json").write_text(
        json.dumps(downloads, indent=2) + "\n", encoding="utf-8")
    (out / ".nojekyll").touch()   # serve files as they are, no Jekyll pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the MyEditor install guide.")
    parser.add_argument("--out", required=True, type=Path, help="output folder (replaced)")
    parser.add_argument("--repo", default=None, help="owner/name (default: APP_REPO_SLUG)")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--offline", action="store_true", help="skip the release lookup")
    source.add_argument("--release-json", type=Path, help="use a saved API response instead")
    args = parser.parse_args(argv)

    if args.release_json:
        release = json.loads(args.release_json.read_text(encoding="utf-8"))
    elif args.offline:
        release = None
    else:
        release = fetch_latest_release(args.repo or repo_slug(), os.environ.get("GITHUB_TOKEN"))

    build_site(args.out, release)
    count = len(build_downloads(release)["files"]) if release else 0
    print(f"Built the install guide in {args.out} ({count} release files).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
