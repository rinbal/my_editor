# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the one rule that maps release files to systems.

The in-app updater and the web install guide both pick files through
release_assets, so a file name the packaging scripts produce must map to
exactly one key, and the updater must ask for the key of the install it is
running from. A wrong-arch match would replace a working app with one that
cannot start, so the arch is part of every key.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import updater  # noqa: E402
from release_assets import appimage_key, asset_key, deb_key, mac_key  # noqa: E402

# The v3.2 release, as the GitHub API lists it.
RELEASE_FILES = {
    "my-editor-3.2-linux-x86_64.AppImage": "appimage-x86_64",
    "my-editor-3.2-macos-arm64.dmg": "mac-arm64",
    "my-editor-3.2-macos-x86_64.dmg": "mac-x86_64",
    "my-editor-3.2-windows-setup.exe": "windows-x64",
    "my-editor_3.2_amd64.deb": "deb-amd64",
}


@pytest.mark.parametrize("name, key", sorted(RELEASE_FILES.items()))
def test_every_shipped_file_has_its_key(name, key):
    assert asset_key(name) == key


@pytest.mark.parametrize("name", [
    "", "SHA256SUMS", "my-editor-3.2.tar.gz", "source.zip", "notes.md",
    "my-editor-3.2-windows-setup.exe.sig",
])
def test_anything_else_has_no_key(name):
    assert asset_key(name) is None


def test_machine_names_match_the_packaging_names():
    assert mac_key("arm64") == "mac-arm64"
    assert mac_key("x86_64") == "mac-x86_64"
    assert appimage_key("x86_64") == "appimage-x86_64"
    assert deb_key("x86_64") == "deb-amd64"      # Debian says amd64
    assert deb_key("aarch64") == "deb-arm64"
    assert mac_key("AMD64") == "mac-x86_64"      # Windows spells it this way


def _assets():
    return [SimpleNamespace(name=name, url=f"https://github.com/x/{name}", size=1)
            for name in RELEASE_FILES]


@pytest.mark.parametrize("kind, machine, expected", [
    (updater.WINDOWS_INSTALLER, "AMD64", "my-editor-3.2-windows-setup.exe"),
    (updater.APPIMAGE, "x86_64", "my-editor-3.2-linux-x86_64.AppImage"),
    (updater.MACOS_APP, "arm64", "my-editor-3.2-macos-arm64.dmg"),
    (updater.MACOS_APP, "x86_64", "my-editor-3.2-macos-x86_64.dmg"),
    (updater.DEB, "x86_64", "my-editor_3.2_amd64.deb"),
])
def test_updater_picks_the_file_for_its_own_install(kind, machine, expected):
    assert updater.select_asset(kind, _assets(), machine).name == expected


def test_updater_never_picks_a_wrong_arch_build():
    assert updater.select_asset(updater.APPIMAGE, _assets(), "aarch64") is None
    assert updater.select_asset(updater.DEB, _assets(), "aarch64") is None


def test_source_and_bare_folders_have_no_file():
    assert updater.select_asset(updater.SOURCE, _assets(), "x86_64") is None
    assert updater.select_asset(updater.LINUX_OTHER, _assets(), "x86_64") is None


def test_a_deb_install_is_recognised_by_where_it_lives(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/opt/my-editor/my-editor")
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    assert updater.detect_install_kind() == updater.DEB

    monkeypatch.setattr(sys, "executable", "/home/me/apps/my-editor/my-editor")
    assert updater.detect_install_kind() == updater.LINUX_OTHER


def test_only_windows_and_writable_appimages_update_themselves(monkeypatch, tmp_path):
    assert updater.supports_in_app_update(updater.WINDOWS_INSTALLER)
    for kind in (updater.MACOS_APP, updater.DEB, updater.LINUX_OTHER, updater.SOURCE):
        assert not updater.supports_in_app_update(kind)
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "my-editor.AppImage"))
    assert updater.supports_in_app_update(updater.APPIMAGE)
