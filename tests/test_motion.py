# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reduce Motion: asked of macOS, Windows and GTK the way their own toolkits
ask, kept on the application, and read again after the app returns.

Every system is played by a fake, so the whole file runs on any computer;
GSettings by a small ``gsettings`` script run as the real one would be.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication

from nostr.ui import motion, native_appearance as na


def _forget(app) -> None:
    kept = getattr(app, "_motion_preference", None)
    if kept is not None:
        del app._motion_preference
        kept.deleteLater()


@pytest.fixture(autouse=True)
def fresh_preference():
    """Each test asks anew; none leaves an answer behind for the next."""
    app = QApplication.instance()
    _forget(app)
    yield
    _forget(app)


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


@pytest.fixture
def gsettings(monkeypatch, tmp_path, gtk):
    """A ``gsettings`` that prints ``answer`` after ``delay`` seconds and
    exits with ``code``, all read from the environment it is given (which
    is the app's ``program_environment``, as for the real one)."""
    if sys.platform == "win32":
        pytest.skip("GSettings is asked on GTK desktops")
    program = tmp_path / "bin" / "gsettings"
    program.parent.mkdir()
    program.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SEEN"\nsleep "$DELAY"\n'
                       'echo "$ANSWER"\nexit "$CODE"\n', encoding="utf-8")
    program.chmod(0o755)
    seen = tmp_path / "seen"
    said = {"ANSWER": "false", "DELAY": "0", "CODE": "0"}
    import rmd_toolchain
    monkeypatch.setattr(rmd_toolchain, "program_environment",
                        lambda: dict(os.environ, SEEN=str(seen), **said))
    monkeypatch.setattr(na.shutil, "which", lambda name: str(program))
    # A busy test machine starts a shell slower than dconf answers.
    monkeypatch.setattr(na, "GSETTINGS_TIMEOUT_MS", 5_000)
    return SimpleNamespace(said=said, runs=lambda: seen.read_text(encoding="utf-8").splitlines()
                           if seen.exists() else [])


def _gtk_preference():
    return na._MotionPreference(QApplication.instance(), "linux")


def _until(done, seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def test_gsettings_output_is_read_as_a_boolean():
    assert na._gsettings_answer("false\n") is False
    assert na._gsettings_answer("true\n") is True
    assert na._gsettings_answer("uint32 0\n") is None
    assert na._gsettings_answer("") is None


def test_gtk_reads_gnomes_switch_through_gsettings_without_waiting_for_it(gsettings):
    gsettings.said["DELAY"] = "0.3"
    preference = _gtk_preference()
    started = time.monotonic()
    # Nothing set in the settings.ini files: motion, at once.
    assert preference.value() is False
    assert time.monotonic() - started < 0.2
    _until(lambda: preference._process is None)
    assert preference.value() is True
    assert gsettings.runs() == ["get org.gnome.desktop.interface enable-animations"]


def test_gtk_animations_on_in_gsettings_keeps_motion(gsettings, gtk):
    gsettings.said["ANSWER"] = "true"
    # Even a settings.ini that says otherwise: GSettings is what GTK reads
    # when a desktop provides it. Until it answers, the file counts.
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=false\n")
    preference = _gtk_preference()
    assert preference.value() is True
    _until(lambda: preference._process is None)
    assert preference.value() is False


@pytest.mark.parametrize("said", [
    {"CODE": "1", "ANSWER": ""},       # No such schema
    {"ANSWER": "uint32 0"},            # not a boolean
])
def test_gtk_falls_back_to_settings_ini_when_gsettings_gives_no_answer(gsettings, gtk, said):
    gsettings.said.update(said)
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=false\n")
    preference = _gtk_preference()
    preference.value()
    _until(lambda: preference._process is None)
    assert preference.value() is True


def test_a_gsettings_that_cannot_start_falls_back_to_settings_ini(monkeypatch, gtk, tmp_path):
    missing = tmp_path / "no-such-gsettings"
    monkeypatch.setattr(na.shutil, "which", lambda name: str(missing))
    _settings_ini(gtk.home, "gtk-3.0", "[Settings]\ngtk-enable-animations=false\n")
    preference = _gtk_preference()
    assert preference.value() is True
    _until(lambda: preference._process is None)
    assert preference._process is None and preference.value() is True


def test_a_gsettings_that_hangs_is_stopped_and_the_last_answer_stays(monkeypatch, gsettings):
    preference = _gtk_preference()
    preference.value()
    _until(lambda: preference._process is None)
    assert preference.value() is True                    # GSettings said off
    monkeypatch.setattr(na, "GSETTINGS_TIMEOUT_MS", 300)
    gsettings.said.update(DELAY="30", ANSWER="true")
    preference.stale()
    started = time.monotonic()
    assert preference.value() is True                    # kept while it runs
    _until(lambda: preference._process is None)
    assert time.monotonic() - started < 2
    assert preference.value() is True                    # no answer changes nothing


def test_after_the_app_returns_the_last_answer_counts_until_gsettings_answers(gsettings):
    preference = _gtk_preference()
    preference.value()
    _until(lambda: preference._process is None)
    assert preference.value() is True
    gsettings.said.update(ANSWER="true", DELAY="0.2")
    preference.stale()
    assert preference.value() is True
    # Asked again while it runs: no second run.
    preference.stale()
    assert preference.value() is True
    _until(lambda: preference._process is None)
    assert preference.value() is False
    assert len(gsettings.runs()) == 2


def test_asked_from_another_thread_it_says_what_is_kept_and_starts_nothing(monkeypatch):
    monkeypatch.setattr(na, "_read_reduce_motion", lambda platform="": True)
    answers = []
    worker = threading.Thread(target=lambda: answers.append(na.reduce_motion_preference()))
    worker.start()
    worker.join()
    assert answers == [False]
    assert not hasattr(QApplication.instance(), "_motion_preference")
    assert na.reduce_motion_preference() is True


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
    monkeypatch.setattr(na.sys, "platform", "darwin")
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
