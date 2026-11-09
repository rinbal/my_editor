# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reduce Motion: asked of macOS, Windows and GTK the way their own toolkits
ask, kept on the application, and read again after the app returns.

Every system is played by a fake, so the whole file runs on any computer.
"""
from __future__ import annotations

import ctypes
import subprocess
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from nostr.ui import motion, native_appearance as na


@pytest.fixture(autouse=True)
def fresh_preference():
    """Each test asks anew; none leaves an answer behind for the next."""
    app = QApplication.instance()
    if hasattr(app, "_reduce_motion_preference"):
        del app._reduce_motion_preference
    yield
    if hasattr(app, "_reduce_motion_preference"):
        del app._reduce_motion_preference


# -- macOS ---------------------------------------------------------------------

def test_macos_reads_reduce_motion_from_nsworkspace(monkeypatch):
    monkeypatch.setattr(na, "_workspace",
                        lambda: SimpleNamespace(accessibilityDisplayShouldReduceMotion=True))
    assert na._read_reduce_motion("darwin") is True
    monkeypatch.setattr(na, "_workspace",
                        lambda: SimpleNamespace(accessibilityDisplayShouldReduceMotion=False))
    assert na._read_reduce_motion("darwin") is False


def test_macos_without_the_cocoa_platform_or_the_property_keeps_motion(monkeypatch):
    monkeypatch.setattr(na, "_workspace", lambda: None)
    assert na._read_reduce_motion("darwin") is False
    monkeypatch.setattr(na, "_workspace", lambda: SimpleNamespace())
    assert na._read_reduce_motion("darwin") is False


@pytest.mark.parametrize("platform,qt_platform", [
    ("darwin", "offscreen"), ("win32", "cocoa"), ("linux", "xcb"),
])
def test_nsworkspace_is_asked_only_on_macos_under_cocoa(monkeypatch, platform, qt_platform):
    app = SimpleNamespace(platformName=lambda: qt_platform)
    monkeypatch.setattr(na, "QApplication", SimpleNamespace(instance=lambda: app))
    monkeypatch.setattr(na.sys, "platform", platform)
    assert na._workspace() is None


# -- Windows -------------------------------------------------------------------

def _fake_user32(monkeypatch, *, enabled: bool, succeeds: bool = True):
    calls = []

    def system_parameters_info(action, ui_param, pv_param, win_ini):
        calls.append((action, ui_param, win_ini))
        if succeeds:
            pv_param._obj.value = enabled
        return 1 if succeeds else 0

    monkeypatch.setattr(ctypes, "WinDLL",
                        lambda name: SimpleNamespace(SystemParametersInfoW=system_parameters_info),
                        raising=False)
    return calls


def test_windows_animation_effects_off_means_reduce_motion(monkeypatch):
    calls = _fake_user32(monkeypatch, enabled=False)
    assert na._read_reduce_motion("win32") is True
    assert calls == [(0x1042, 0, 0)]


def test_windows_animation_effects_on_keeps_motion(monkeypatch):
    _fake_user32(monkeypatch, enabled=True)
    assert na._read_reduce_motion("win32") is False


def test_windows_failed_call_keeps_motion(monkeypatch):
    # As in Chromium: no answer is no request to take motion away.
    _fake_user32(monkeypatch, enabled=False, succeeds=False)
    assert na._read_reduce_motion("win32") is False


def test_windows_without_user32_keeps_motion(monkeypatch):
    def missing(name):
        raise OSError("no user32 here")

    monkeypatch.setattr(ctypes, "WinDLL", missing, raising=False)
    assert na._read_reduce_motion("win32") is False


# -- GTK -----------------------------------------------------------------------

@pytest.fixture
def gtk(monkeypatch, tmp_path):
    """A desktop with no gsettings and empty GTK config folders, unless a
    test gives it one."""
    home, system = tmp_path / "config", tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(system))
    monkeypatch.setattr(na, "_gtk_settings_files",
                        _without_etc(na._gtk_settings_files))
    monkeypatch.setattr(na.shutil, "which", lambda name: None)
    return SimpleNamespace(home=home, system=system)


def _without_etc(files):
    # The computer's own /etc is not part of the fake desktop.
    return lambda: [f for f in files() if not f.startswith("/etc/")]


def _settings_ini(folder, version: str, text: str) -> None:
    path = folder / version / "settings.ini"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _fake_gsettings(monkeypatch, *, stdout: str = "", returncode: int = 0, raises=None):
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(na.shutil, "which", lambda name: "/usr/bin/gsettings")
    monkeypatch.setattr(na.subprocess, "run", run)
    return seen


def test_gtk_reads_gnomes_switch_through_gsettings(monkeypatch, gtk):
    seen = _fake_gsettings(monkeypatch, stdout="false\n")
    assert na._read_reduce_motion("linux") is True
    args, kwargs = seen[0]
    assert args == ["/usr/bin/gsettings", "get", "org.gnome.desktop.interface",
                    "enable-animations"]
    assert kwargs["timeout"] <= 1.0
    # The person's own environment, not a packaged build's libraries.
    assert "env" in kwargs


def test_gtk_animations_on_in_gsettings_keeps_motion(monkeypatch, gtk):
    _fake_gsettings(monkeypatch, stdout="true\n")
    # Even a settings.ini that says otherwise: GSettings is what GTK reads
    # when a desktop provides it.
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=false\n")
    assert na._read_reduce_motion("linux") is False


@pytest.mark.parametrize("outcome", [
    {"raises": subprocess.TimeoutExpired(["gsettings"], 1.0)},
    {"raises": OSError("cannot run")},
    {"returncode": 1, "stdout": ""},   # No such schema
    {"stdout": "uint32 0\n"},          # not a boolean
])
def test_gtk_falls_back_to_settings_ini_when_gsettings_gives_no_answer(monkeypatch, gtk, outcome):
    _fake_gsettings(monkeypatch, **outcome)
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=false\n")
    assert na._read_reduce_motion("linux") is True


@pytest.mark.parametrize("value,reduced", [
    ("false", True), ("0", True), ("true", False), ("1", False),
    ("  false  ", True),
])
def test_gtk_settings_ini_values_read_as_glib_reads_booleans(gtk, value, reduced):
    _settings_ini(gtk.home, "gtk-3.0", f"[Settings]\ngtk-enable-animations={value}\n")
    assert na._read_reduce_motion("linux") is reduced


def test_gtk_the_persons_own_file_wins_over_the_systems(gtk):
    _settings_ini(gtk.system, "gtk-3.0", "[Settings]\ngtk-enable-animations=false\n")
    assert na._read_reduce_motion("linux") is True
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=true\n")
    assert na._read_reduce_motion("linux") is False


def test_gtk_4_settings_come_before_gtk_3(gtk):
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=true\n")
    _settings_ini(gtk.home, "gtk-4.0", "[Settings]\ngtk-enable-animations=false\n")
    assert na._read_reduce_motion("linux") is True


def test_gtk_a_file_without_the_setting_or_unreadable_is_passed_over(gtk):
    _settings_ini(gtk.home, "gtk-4.0", "[Settings]\ngtk-theme-name=Adwaita\n")
    _settings_ini(gtk.home, "gtk-3.0", "not a key file [[[\n")
    _settings_ini(gtk.system, "gtk-3.0", "[Settings]\ngtk-enable-animations=maybe\n")
    assert na._read_reduce_motion("linux") is False
    _settings_ini(gtk.system, "gtk-4.0", "[Settings]\ngtk-enable-animations=0\n")
    assert na._read_reduce_motion("linux") is True


def test_gtk_nothing_set_anywhere_keeps_motion(gtk):
    assert na._read_reduce_motion("linux") is False


def test_gtk_settings_files_are_where_gtks_documentation_puts_them(monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/a/.config")
    monkeypatch.setenv("XDG_CONFIG_DIRS", "/etc/xdg:/usr/local/etc/xdg")
    assert na._gtk_settings_files() == [
        "/home/a/.config/gtk-4.0/settings.ini", "/home/a/.config/gtk-3.0/settings.ini",
        "/etc/xdg/gtk-4.0/settings.ini", "/etc/xdg/gtk-3.0/settings.ini",
        "/usr/local/etc/xdg/gtk-4.0/settings.ini", "/usr/local/etc/xdg/gtk-3.0/settings.ini",
        "/etc/gtk-4.0/settings.ini", "/etc/gtk-3.0/settings.ini",
    ]


# -- kept, and read again ------------------------------------------------------

def test_the_answer_is_kept_and_read_again_after_the_app_returns(monkeypatch):
    answers = iter([True, False])
    reads = []

    def read(platform=""):
        reads.append(platform)
        return next(answers)

    monkeypatch.setattr(na, "_read_reduce_motion", read)
    assert motion.reduce_motion() is True
    assert motion.reduce_motion() is True
    assert len(reads) == 1
    # The person changed it in the system's settings and came back.
    QApplication.instance()._night_violet_observer.refresh(Qt.ApplicationActive)
    assert len(reads) == 1  # not read until an animation asks
    assert motion.reduce_motion() is False
    assert len(reads) == 2


# -- durations -----------------------------------------------------------------

def test_durations_as_designed_when_motion_is_not_reduced(monkeypatch):
    monkeypatch.setattr(na, "_read_reduce_motion", lambda platform="": False)
    assert motion.duration(160) == 160
    assert motion.duration(320, cross_fade=True) == 320
    assert motion.duration(-5) == 0


def test_reduced_motion_moves_nothing_and_cross_fades_what_should_be_seen(monkeypatch):
    monkeypatch.setattr(na, "_read_reduce_motion", lambda platform="": True)
    assert motion.duration(160) == 0
    assert motion.duration(320, cross_fade=True) == motion.CROSS_FADE_MS == 120
