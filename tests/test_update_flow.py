# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins what Software Update tells each kind of install to do.

Updating has to follow the same steps as the install guide, so for every
install the plan must pick the right mode, the guide link must open the
guide on the right system in update mode, and the words must name the
buttons people will actually see. Nothing here opens a window.
"""

import os
import sys
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import updater  # noqa: E402
from constants import APP_INSTALL_GUIDE_URL  # noqa: E402
from update_flow import AUTOMATIC, FROM_SOURCE, GUIDED, guide_url, plan_for  # noqa: E402

RELEASE_URL = "https://github.com/rinbal/my_editor/releases/tag/v3.3"


def _query(url):
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == APP_INSTALL_GUIDE_URL
    return {key: values[0] for key, values in parse_qs(parts.query).items()}


def test_the_guide_url_is_the_pages_site_of_the_repo():
    assert APP_INSTALL_GUIDE_URL == "https://rinbal.github.io/my_editor/install/"


@pytest.mark.parametrize("kind, machine, expected", [
    (updater.MACOS_APP, "arm64", {"os": "mac", "arch": "arm64", "update": "3.3"}),
    (updater.MACOS_APP, "x86_64", {"os": "mac", "arch": "x86_64", "update": "3.3"}),
    (updater.WINDOWS_INSTALLER, "AMD64", {"os": "windows", "update": "3.3"}),
    (updater.DEB, "x86_64", {"os": "linux", "package": "deb", "update": "3.3"}),
    (updater.APPIMAGE, "x86_64", {"os": "linux", "package": "appimage", "update": "3.3"}),
])
def test_the_guide_opens_on_this_install(kind, machine, expected):
    assert _query(guide_url(kind, update_to="3.3", machine=machine)) == expected


def test_installation_help_opens_the_guide_in_install_mode():
    assert _query(guide_url(updater.MACOS_APP, machine="arm64")) == {"os": "mac", "arch": "arm64"}


def test_a_source_checkout_opens_the_guide_for_its_own_system():
    assert _query(guide_url(updater.SOURCE, sys_platform="darwin"))["os"] == "mac"
    assert _query(guide_url(updater.SOURCE, sys_platform="win32"))["os"] == "windows"
    assert _query(guide_url(updater.SOURCE, sys_platform="linux"))["os"] == "linux"


def test_self_updating_installs_get_the_automatic_plan():
    asset = SimpleNamespace(name="my-editor-3.3-windows-setup.exe", size=41_545_251)
    plan = plan_for(updater.WINDOWS_INSTALLER, "3.3", release_url=RELEASE_URL,
                    asset=asset, can_self_update=True, machine="AMD64")
    assert plan.mode == AUTOMATIC
    assert plan.primary_label == "Update Now"
    assert [s.title for s in plan.steps] == ["Download", "Save your work", "Restart"]
    assert "(42 MB)" in plan.steps[0].detail
    assert _query(plan.guide_url)["os"] == "windows"   # the fallback if it fails


def test_the_appimage_restart_step_says_what_happens_to_the_file():
    plan = plan_for(updater.APPIMAGE, "3.3", release_url=RELEASE_URL,
                    asset=None, can_self_update=True, machine="x86_64")
    assert "AppImage" in plan.steps[-1].detail


def test_a_mac_repeats_the_install_steps_through_the_guide():
    plan = plan_for(updater.MACOS_APP, "3.3", release_url=RELEASE_URL, machine="arm64")
    assert plan.mode == GUIDED
    assert plan.primary_label == "Open Update Guide"
    text = " ".join(s.detail for s in plan.steps)
    for label in ("Replace", "Done", "Open Anyway", "Privacy & Security"):
        assert label in text
    assert _query(plan.guide_url) == {"os": "mac", "arch": "arm64", "update": "3.3"}


def test_the_deb_plan_names_the_real_file_in_its_command():
    asset = SimpleNamespace(name="my-editor_3.3_amd64.deb", size=1)
    plan = plan_for(updater.DEB, "3.3", release_url=RELEASE_URL, asset=asset, machine="x86_64")
    assert plan.mode == GUIDED
    assert plan.steps[1].command == "sudo apt install ./my-editor_3.3_amd64.deb"


def test_windows_falls_back_to_the_guide_when_it_cannot_update_itself():
    plan = plan_for(updater.WINDOWS_INSTALLER, "3.3", release_url=RELEASE_URL,
                    can_self_update=False, machine="AMD64")
    assert plan.mode == GUIDED
    assert "Run anyway" in " ".join(s.detail for s in plan.steps)


def test_a_read_only_appimage_is_told_why_it_needs_a_manual_swap():
    plan = plan_for(updater.APPIMAGE, "3.3", release_url=RELEASE_URL,
                    can_self_update=False, machine="x86_64")
    assert plan.mode == GUIDED
    assert "read-only" in plan.intro


def test_a_source_checkout_is_updated_with_git():
    plan = plan_for(updater.SOURCE, "3.3", release_url=RELEASE_URL)
    assert plan.mode == FROM_SOURCE
    assert plan.steps[0].command == "git pull"
    assert plan.guide_url == RELEASE_URL
    assert plan.primary_label == "Release Notes"


def test_no_plan_text_uses_an_em_dash():
    kinds = (updater.MACOS_APP, updater.DEB, updater.WINDOWS_INSTALLER,
             updater.APPIMAGE, updater.LINUX_OTHER, updater.SOURCE)
    for kind in kinds:
        for can_self_update in (False, True):
            plan = plan_for(kind, "3.3", release_url=RELEASE_URL,
                            can_self_update=can_self_update, machine="x86_64")
            text = plan.intro + "".join(s.title + s.detail + s.command for s in plan.steps)
            assert "\u2014" not in text
