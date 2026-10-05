#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What updating takes for each kind of install, as plain data.

The Software Update dialog (update_dialog.py) renders a plan from here, and
the web install guide (site/install/) shows the same steps with pictures.
Keeping the words next to the install kinds means the dialog and the guide
describe one flow, and the copy can be tested without a window.

Three modes:

    AUTOMATIC    MyEditor downloads the update, checks it, closes, and
                 reopens on it with every tab as it was (Windows installer,
                 a writable AppImage, a Mac app in a writable folder, the
                 .deb when the system can ask for a password).
    GUIDED       Updating repeats the install steps, so the dialog lists them
                 and hands off to the install guide in update mode (any
                 install that cannot replace itself).
    FROM_SOURCE  A git checkout: the steps are commands.
"""

import sys
from dataclasses import dataclass
from urllib.parse import urlencode

from constants import APP_INSTALL_GUIDE_URL
from release_assets import normalize_machine
from updater import APPIMAGE, DEB, MACOS_APP, SOURCE, WINDOWS_INSTALLER

AUTOMATIC = "automatic"
GUIDED = "guided"
FROM_SOURCE = "source"


# What a step of an AUTOMATIC plan does, so the dialog can find the row
# to update without counting on how many steps a plan has.
DOWNLOAD = "download"
PREPARE = "prepare"
RESTART = "restart"


@dataclass(frozen=True)
class Step:
    title: str
    detail: str
    command: str = ""   # a shell line shown with a Copy button
    role: str = ""      # DOWNLOAD, PREPARE or RESTART in an AUTOMATIC plan


@dataclass(frozen=True)
class UpdatePlan:
    mode: str
    intro: str
    steps: tuple        # tuple[Step, ...]
    primary_label: str  # the default button
    guide_url: str      # what the primary button opens (GUIDED, FROM_SOURCE),
                        # and the manual fallback when AUTOMATIC fails


def guide_url(kind: str, *, update_to: str = None, machine: str = None,
              sys_platform: str = None) -> str:
    """The install guide, opened on the right system (and in update mode).

    The app knows exactly how it was installed, so it says so in the query
    string instead of leaving the page to guess from the browser.
    """
    params = {"os": _guide_os(kind, sys_platform or sys.platform)}
    if params["os"] == "mac" and machine:
        params["arch"] = normalize_machine(machine)
    if kind == DEB:
        params["package"] = "deb"
    elif kind == APPIMAGE:
        params["package"] = "appimage"
    if update_to:
        params["update"] = update_to
    return f"{APP_INSTALL_GUIDE_URL}?{urlencode(params)}"


def plan_for(kind: str, version: str, *, release_url: str, asset=None,
             can_self_update: bool = False, machine: str = None,
             sys_platform: str = None) -> UpdatePlan:
    """The update plan for this install. ``asset`` is the matching release file."""
    guide = guide_url(kind, update_to=version, machine=machine,
                      sys_platform=sys_platform)
    if kind == SOURCE:
        return _source_plan(release_url)
    if can_self_update:
        return _automatic_plan(kind, version, asset, guide)
    return _guided_plan(kind, version, asset, guide)


# -- plans ------------------------------------------------------------------

def _automatic_plan(kind, version, asset, guide) -> UpdatePlan:
    size = _megabytes(getattr(asset, "size", 0))
    download = "MyEditor downloads the update from GitHub"
    download += f" ({size} MB)" if size else ""
    download += " and checks that it arrived intact."
    steps = [Step("Download", download, role=DOWNLOAD)]
    if kind == MACOS_APP:
        steps.append(Step("Install", "MyEditor puts the new version next to this one "
                                     "and checks its signature.", role=PREPARE))
    elif kind == DEB:
        steps.append(Step("Install", "Your system asks for your password, "
                                     "then installs the update.", role=PREPARE))
    if kind == WINDOWS_INSTALLER:
        restart = "The installer replaces this version and opens MyEditor again"
    elif kind == APPIMAGE:
        restart = "MyEditor swaps in the new AppImage and opens again"
    else:
        restart = "MyEditor closes and opens again on the new version"
    steps.append(Step("Restart", restart + ", with your tabs just as you left them.",
                      role=RESTART))
    return UpdatePlan(
        mode=AUTOMATIC,
        intro=(f"MyEditor installs version {version} and opens again. Every open "
               "document comes back, including changes you haven't saved."),
        steps=tuple(steps),
        primary_label="Install Update",
        guide_url=guide,
    )


def _guided_plan(kind, version, asset, guide) -> UpdatePlan:
    if kind == MACOS_APP:
        intro = ("On a Mac, updating takes the same steps as installing. "
                 "The update guide shows each one with pictures.")
        steps = (
            Step("Download", "Download the new disk image from the update guide."),
            Step("Replace", "Quit MyEditor. Open the disk image, drag MyEditor "
                            "onto Applications, and click Replace."),
            Step("Open", "Open MyEditor. If macOS says it can't verify it, click "
                         "Done, then click Open Anyway in System Settings > "
                         "Privacy & Security."),
        )
    elif kind == DEB:
        name = getattr(asset, "name", "") or f"my-editor_{version}_amd64.deb"
        intro = "The .deb package updates the same way it installs."
        steps = (
            Step("Download", "Download the new .deb package from the update guide."),
            Step("Install", "Quit MyEditor, open the package, and click Install. "
                            "Or run this in a terminal, in your Downloads folder:",
                 command=f"sudo apt install ./{name}"),
            Step("Open", "Open MyEditor again from your applications menu."),
        )
    elif kind == WINDOWS_INSTALLER:
        intro = ("Updating takes the same steps as installing. "
                 "The update guide shows each one with pictures.")
        steps = (
            Step("Download", "Download the new installer from the update guide."),
            Step("Run", "Open it. If “Windows protected your PC” appears, "
                        "click More info, then Run anyway."),
            Step("Install", "Click through the installer. It replaces the old "
                            "version and keeps your settings."),
        )
    elif kind == APPIMAGE:
        intro = ("MyEditor can't replace its AppImage because the folder it's in "
                 "is read-only, so this update is a quick manual swap.")
        steps = (
            Step("Download", "Download the new AppImage from the update guide."),
            Step("Replace", "Quit MyEditor and put the new file where the old one "
                            "was. Turn on Executable as Program in its Properties."),
            Step("Open", "Double-click the new AppImage."),
        )
    else:
        intro = "Updating takes the same steps as installing."
        steps = (
            Step("Download", "Download the new version from the update guide."),
            Step("Install", "Install it the same way you installed this copy."),
            Step("Open", "Open MyEditor again."),
        )
    return UpdatePlan(GUIDED, intro, steps, "Open Update Guide", guide)


def _source_plan(release_url) -> UpdatePlan:
    return UpdatePlan(
        mode=FROM_SOURCE,
        intro="This copy runs from source code, so git updates it.",
        steps=(
            Step("Get the new code", "In the MyEditor folder, run:", command="git pull"),
            Step("Update dependencies", "Then run:",
                 command="pip install -r requirements.txt"),
            Step("Restart", "Quit MyEditor and start it again."),
        ),
        primary_label="Release Notes",
        guide_url=release_url,
    )


# -- helpers ----------------------------------------------------------------

def _guide_os(kind: str, sys_platform: str) -> str:
    if kind == MACOS_APP:
        return "mac"
    if kind == WINDOWS_INSTALLER:
        return "windows"
    if kind == SOURCE:
        if sys_platform == "darwin":
            return "mac"
        if sys_platform == "win32":
            return "windows"
    return "linux"


def _megabytes(size) -> int:
    try:
        return round(int(size) / 1_000_000)
    except (TypeError, ValueError):
        return 0
