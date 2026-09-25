#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Short keys for the installers a release ships.

The packaging scripts name every installer after the system and CPU it is
for (see packaging/README.md). This module turns such a file name into a
short key, so the in-app updater and the web install guide pick files by
one rule instead of two lists that can drift apart:

    my-editor-3.2-macos-arm64.dmg          -> "mac-arm64"
    my-editor-3.2-macos-x86_64.dmg         -> "mac-x86_64"
    my-editor-3.2-windows-setup.exe        -> "windows-x64"
    my-editor_3.2_amd64.deb                -> "deb-amd64"
    my-editor-3.2-linux-x86_64.AppImage    -> "appimage-x86_64"

Pure stdlib on purpose: the install guide build runs it in CI without Qt.
"""

import re

WINDOWS = "windows-x64"

_PATTERNS = (
    (re.compile(r"-macos-(?P<arch>[a-z0-9_]+)\.dmg$"), "mac-{arch}"),
    (re.compile(r"-windows-setup\.exe$"), WINDOWS),
    (re.compile(r"_(?P<arch>[a-z0-9_]+)\.deb$"), "deb-{arch}"),
    (re.compile(r"-linux-(?P<arch>[a-z0-9_]+)\.appimage$"), "appimage-{arch}"),
)

# Debian names CPUs differently from the kernel (and from platform.machine()).
_DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}


def asset_key(name: str):
    """Key for a release file name, or None for anything that is not an installer."""
    lowered = (name or "").lower()
    for pattern, template in _PATTERNS:
        match = pattern.search(lowered)
        if match:
            return template.format(**match.groupdict())
    return None


def normalize_machine(machine: str) -> str:
    """platform.machine() as the packaging scripts spell it (Windows says AMD64)."""
    lowered = (machine or "").lower()
    return "x86_64" if lowered == "amd64" else lowered


def mac_key(machine: str) -> str:
    return f"mac-{normalize_machine(machine)}"


def appimage_key(machine: str) -> str:
    return f"appimage-{normalize_machine(machine)}"


def deb_key(machine: str) -> str:
    arch = normalize_machine(machine)
    return f"deb-{_DEB_ARCH.get(arch, arch)}"
