# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the install guide (site/) and the script that builds it.

The guide is plain HTML that guide.js turns into one step at a time, so
its structure is its contract:

  Every system has steps with ids, short labels and headings, starts with
  a download and ends on a finished state.

  Every download button asks for a file key the release actually has
  (release_assets.py), or the button would silently keep its fallback.

  The Mac and Linux tracks work in update mode, because Software Update
  sends people there.

  Every local file the page loads exists after a build, and the build
  writes downloads.json from a release without trusting other hosts.

The JavaScript helpers are covered by tests/site/platform.test.mjs, run
here when Node is installed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging" / "site"))

import build_site  # noqa: E402

SITE = ROOT / "site"
PAGE = SITE / "install" / "index.html"
RELEASE_KEYS = {"mac-arm64", "mac-x86_64", "windows-x64", "deb-amd64", "appimage-x86_64"}
BUILT_FILES = {"icon.png", "downloads.json"}   # added by build_site.py


class _Page(HTMLParser):
    """Collects what the tests need from index.html in one pass."""

    def __init__(self):
        super().__init__()
        self.elements = []          # (tag, attrs, track id, step id)
        self._track = None
        self._step = None
        self._stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "section" and "track" in classes:
            self._track = attrs["id"]
        if tag == "li" and "step" in classes:
            self._step = attrs["id"]
        self.elements.append((tag, attrs, self._track, self._step))
        if tag not in ("meta", "link", "img", "br", "hr", "input"):
            self._stack.append((tag, "track" in classes, "step" in classes))

    def handle_endtag(self, tag):
        while self._stack:
            open_tag, was_track, was_step = self._stack.pop()
            if was_step:
                self._step = None
            if was_track:
                self._track = None
            if open_tag == tag:
                break


@pytest.fixture(scope="module")
def page():
    parser = _Page()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    return parser


def _steps(page, track=None):
    return [(attrs, t) for tag, attrs, t, _ in page.elements
            if tag == "li" and "step" in (attrs.get("class") or "").split()
            and (track is None or t == track)]


def test_there_is_a_track_for_each_system(page):
    tracks = [attrs for tag, attrs, *_ in page.elements
              if tag == "section" and "track" in (attrs.get("class") or "")]
    assert [(t["id"], t["data-os"]) for t in tracks] == [
        ("mac", "mac"), ("windows", "windows"), ("linux", "linux")]


@pytest.mark.parametrize("track", ["mac", "windows", "linux"])
def test_each_track_starts_with_a_download_and_ends_finished(page, track):
    steps = _steps(page, track)
    ids = [attrs["id"] for attrs, _ in steps]
    assert ids[0] == f"{track}-download"
    assert ids[-1] == f"{track}-done"
    assert all(i.startswith(f"{track}-") for i in ids)
    assert all(attrs.get("data-label") for attrs, _ in steps)


def test_step_ids_are_unique_and_every_step_has_a_heading(page):
    ids = [attrs["id"] for attrs, _ in _steps(page)]
    assert len(ids) == len(set(ids))
    with_heading = {step for tag, _, _, step in page.elements if tag == "h3" and step}
    assert with_heading == set(ids)


def test_every_download_asks_for_a_file_the_release_has(page):
    keys = {attrs["data-download"] for _, attrs, *_ in page.elements if "data-download" in attrs}
    keys |= {attrs["data-file-meta"] for _, attrs, *_ in page.elements if "data-file-meta" in attrs}
    keys |= {attrs["data-command-file"] for _, attrs, *_ in page.elements if "data-command-file" in attrs}
    assert keys == RELEASE_KEYS


def test_download_links_fall_back_to_the_release_page(page):
    for tag, attrs, *_ in page.elements:
        if "data-download" in attrs:
            assert attrs["href"] == "https://github.com/rinbal/my_editor/releases/latest"


def test_filter_attributes_use_known_values(page):
    allowed = {"data-mode": {"install", "update"}, "data-arch": {"arm64", "x86_64"},
               "data-package": {"deb", "appimage"}}
    for _, attrs, *_ in page.elements:
        for name, values in allowed.items():
            if name in attrs:
                assert attrs[name] in values, (name, attrs[name])


@pytest.mark.parametrize("track", ["mac", "linux"])
def test_update_mode_has_a_full_path(page, track):
    usable = [attrs["id"] for attrs, _ in _steps(page, track)
              if attrs.get("data-mode") in (None, "update")]
    assert len(usable) >= 3
    assert usable[0].endswith("-download") and usable[-1].endswith("-done")


def test_every_jump_goes_to_a_step_that_exists(page):
    ids = {attrs["id"] for attrs, _ in _steps(page)}
    for _, attrs, *_ in page.elements:
        if "data-go" in attrs:
            assert attrs["data-go"] in ids


def test_every_local_file_the_page_loads_exists():
    html = PAGE.read_text(encoding="utf-8")
    refs = set(re.findall(r'(?:href|src)="([^"#:]+)"', html))
    for js in (SITE / "install").glob("*.js"):
        refs |= set(re.findall(r'from "\./([^"]+)"', js.read_text(encoding="utf-8")))
    css = (SITE / "install" / "guide.css").read_text(encoding="utf-8")
    refs |= {ref for ref in re.findall(r'url\("([^"]+)"\)', css) if not ref.startswith("data:")}
    for ref in refs:
        assert ref in BUILT_FILES or (SITE / "install" / ref).is_file(), ref


def test_no_em_dashes_in_the_site():
    for path in SITE.rglob("*"):
        if path.is_file():
            assert "\u2014" not in path.read_text(encoding="utf-8"), path


# -- build_site.py ------------------------------------------------------------

RELEASE = {
    "tag_name": "v3.3",
    "html_url": "https://github.com/rinbal/my_editor/releases/tag/v3.3",
    "assets": [
        {"name": "my-editor-3.3-macos-arm64.dmg", "size": 49_000_000,
         "browser_download_url": "https://github.com/rinbal/my_editor/releases/download/v3.3/my-editor-3.3-macos-arm64.dmg",
         "digest": "sha256:" + "a" * 64},
        {"name": "my-editor_3.3_amd64.deb", "size": 75_000_000,
         "browser_download_url": "https://github.com/rinbal/my_editor/releases/download/v3.3/my-editor_3.3_amd64.deb"},
        {"name": "my-editor-3.3-windows-setup.exe", "size": 1,
         "browser_download_url": "https://evil.example/my-editor-3.3-windows-setup.exe"},
        {"name": "SHA256SUMS", "size": 1,
         "browser_download_url": "https://github.com/rinbal/my_editor/releases/download/v3.3/SHA256SUMS"},
    ],
}


def test_downloads_are_keyed_like_the_updater_and_carry_checksums():
    downloads = build_site.build_downloads(RELEASE)
    assert downloads["version"] == "3.3"
    assert downloads["page"] == RELEASE["html_url"]
    assert set(downloads["files"]) == {"mac-arm64", "deb-amd64"}
    assert downloads["files"]["mac-arm64"]["sha256"] == "a" * 64
    assert downloads["files"]["deb-amd64"]["sha256"] == ""


def test_files_on_other_hosts_are_dropped():
    downloads = build_site.build_downloads(RELEASE)
    assert "windows-x64" not in downloads["files"]


def test_an_offline_build_is_complete_and_deployable(tmp_path):
    out = tmp_path / "_site"
    build_site.build_site(out, None)
    assert (out / "index.html").is_file()
    assert (out / "install" / "index.html").is_file()
    assert (out / "install" / "icon.png").is_file()
    assert (out / ".nojekyll").is_file()
    assert json.loads((out / "install" / "downloads.json").read_text()) == {
        "version": "", "page": "", "files": {}}


def test_a_build_from_a_saved_release(tmp_path):
    saved = tmp_path / "release.json"
    saved.write_text(json.dumps(RELEASE), encoding="utf-8")
    out = tmp_path / "_site"
    assert build_site.main(["--out", str(out), "--release-json", str(saved)]) == 0
    downloads = json.loads((out / "install" / "downloads.json").read_text())
    assert downloads["files"]["mac-arm64"]["name"] == "my-editor-3.3-macos-arm64.dmg"


def test_the_build_refuses_to_replace_the_repository():
    with pytest.raises(SystemExit):
        build_site.build_site(ROOT, None)
    with pytest.raises(SystemExit):
        build_site.build_site(SITE / "install", None)


def test_the_repo_slug_is_read_from_constants():
    assert build_site.repo_slug() == "rinbal/my_editor"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_the_javascript_helpers():
    result = subprocess.run(
        ["node", "--test", *map(str, sorted((ROOT / "tests" / "site").glob("*.test.mjs")))],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
