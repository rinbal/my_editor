# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""app_links.open_link hands a signer app's link to the system and says
honestly whether an app took it (RG-M2r2b 3 and 4).

What must hold:

  Nothing waits on the GUI thread: the call returns while the opening
  program is still held, the window keeps turning, and the answer comes
  on a later turn, also when the program hangs. Shown with stand-ins
  held until the test lets them go, never with a bound of wall-clock
  time (lesson 34).
  macOS answers by ``open``'s exit. Linux asks xdg-mime first and says
  no app when none is set up (xdg-open would hand the link to a browser
  and report success); with one, xdg-open is started detached (on a
  desktop it does not know it runs the handler in the foreground), and
  when xdg-mime cannot answer the answer is that the system cannot tell.
  Windows reads the registry first (``HKCR\\<scheme>`` with ``URL
  Protocol`` and ``shell\\open\\command``): without a registration it
  says no app, since the shell would show its own prompt and report
  success. Only a link is handed over. The link and its secret are never
  logged, whatever goes wrong.

The programs are stand-ins on PATH, the registry a fake: Windows is not
verified on a device.
"""

import logging
import os
import stat
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app_links  # noqa: E402

SECRET = "5ec4e7c0ffee1234"
LINK = ("nostrconnect://" + "ab" * 32 + "?relay=wss%3A%2F%2Fnos.lol&secret=" + SECRET
        + "&name=MyEditor")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def programs(tmp_path, monkeypatch):
    """Stand-ins for the system's programs, first on PATH: ``make(name,
    body)`` writes one; what each was given is in ``<name>.args``."""
    folder = tmp_path / "bin"
    folder.mkdir()

    class Programs:
        path = folder

        def make(self, name, body):
            script = folder / name
            script.write_text(f'#!/bin/sh\necho "$@" > "{folder}/{name}.args"\n{body}\n',
                              encoding="utf-8")
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

        def args(self, name):
            target = folder / f"{name}.args"
            return target.read_text(encoding="utf-8").strip() if target.exists() else None

        def hold(self, name, code=0):
            """``name`` waits until :meth:`release` (at most 20 s, so a
            window held by it cannot hang the test for ever), then exits
            with ``code``; :meth:`started` and :meth:`ended` say where it is."""
            self.make(name, f'/usr/bin/touch "{folder}/{name}.started"\n'
                            f'i=0; while [ ! -e "{folder}/release" ] && [ $i -lt 400 ]; do '
                            f'/bin/sleep 0.05; i=$((i+1)); done\n'
                            f'/usr/bin/touch "{folder}/{name}.ended"\nexit {code}')

        def release(self):
            (folder / "release").touch()

        def started(self, name):
            return (folder / f"{name}.started").exists()

        def ended(self, name):
            return (folder / f"{name}.ended").exists()

    # Only the stand-ins: a real opener of the system is never reached.
    monkeypatch.setenv("PATH", str(folder))
    return Programs()


def answer_of(uri, platform, monkeypatch, *, registry=None, within_s=8.0):
    """What open_link answers on ``platform``."""
    monkeypatch.setattr(app_links, "_platform", lambda: platform)
    got = []
    loop = QEventLoop()

    def done(answer):
        got.append(answer)
        loop.quit()
    app_links.open_link(uri, done, registry=registry)
    assert got == [], "answered on the GUI thread, before a later turn"
    if not got:
        # A deadline for a test that would otherwise hang, not a wait the
        # answer depends on.
        QTimer.singleShot(int(within_s * 1000), loop.quit)
        loop.exec()
    assert len(got) == 1, "never answered"
    return got[0]


def wait_for(check, within_s=10.0):
    # A deadline for a test that would otherwise hang, not a bound the
    # behaviour is measured by.
    deadline = time.monotonic() + within_s
    while not check() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    return check()


def turns_while(held, ended):
    """The window keeps turning while ``held()`` (a stand-in started and
    let go by the test): a timer of the GUI thread ticks again and again
    before ``ended()``. A wait on the GUI thread for the stand-in would
    let no tick through until it ended by itself."""
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(0)
    try:
        assert wait_for(held), "the stand-in never started"
        seen = len(ticks)
        assert wait_for(lambda: len(ticks) > seen + 3)
        assert not ended(), "the window waited for the program"
    finally:
        timer.stop()


def open_held(monkeypatch, platform, *, registry=None):
    """open_link on ``platform`` with what it opens held: what it answered
    so far (a list), once the call returned."""
    monkeypatch.setattr(app_links, "_platform", lambda: platform)
    got = []
    app_links.open_link(LINK, got.append, registry=registry)
    return got


def test_a_signer_apps_link_is_not_a_url_qt_takes():
    # Why not QDesktopServices: the key is a 64-character host label, one
    # more than a host label may have.
    assert not QUrl(LINK).isValid()
    assert QUrl(LINK.replace("ab" * 32, "a" * 63)).isValid()


# -- macOS --------------------------------------------------------------------

@pytest.mark.parametrize("code, answer", [(0, app_links.OPENED), (1, app_links.NO_APP)])
def test_macos_answers_by_opens_exit(programs, monkeypatch, code, answer):
    programs.make("open", f"exit {code}")
    got = answer_of(LINK, "mac", monkeypatch)
    assert got == answer
    assert programs.args("open") == LINK


def test_macos_never_holds_the_window_while_open_runs(programs, monkeypatch):
    # RG-M2r3 8: only a wall-clock bound proved it, and an open run on the
    # window's next turn passed.
    programs.hold("open")
    got = open_held(monkeypatch, "mac")
    assert got == [] and not programs.ended("open")
    turns_while(lambda: programs.started("open"), lambda: programs.ended("open"))
    assert got == []                                    # open answers by its exit
    programs.release()
    assert wait_for(lambda: got == [app_links.OPENED])


def test_an_opening_that_hangs_never_holds_the_window_and_cannot_tell(programs, monkeypatch):
    programs.make("open", "exec /bin/sleep 30")
    monkeypatch.setattr(app_links, "ANSWER_WAIT_MS", 300)
    got = answer_of(LINK, "mac", monkeypatch)
    assert got == app_links.UNKNOWN
    assert app_links._running == []                     # the child was let go


# -- Linux ----------------------------------------------------------------------

def test_linux_says_no_app_when_none_is_set_up_and_opens_nothing(programs, monkeypatch):
    programs.make("xdg-mime", "exit 0")                 # no default: nothing printed
    programs.make("xdg-open", "exit 0")                 # would hand it to a browser
    got = answer_of(LINK, "linux", monkeypatch)
    assert got == app_links.NO_APP
    assert programs.args("xdg-mime") == "query default x-scheme-handler/nostrconnect"
    assert programs.args("xdg-open") is None


def test_linux_starts_xdg_open_detached_when_an_app_is_set_up(programs, monkeypatch):
    # On a desktop xdg-open does not know (sway, i3), it runs the handler in
    # the foreground: never waited for.
    # RG-M2r3 8: the answer comes while xdg-open is still held, and the
    # window turns meanwhile.
    programs.make("xdg-mime", "echo amber.desktop")
    programs.hold("xdg-open")
    got = open_held(monkeypatch, "linux")
    assert got == [] and not programs.ended("xdg-open")
    assert wait_for(lambda: got == [app_links.OPENED])
    turns_while(lambda: programs.started("xdg-open"), lambda: programs.ended("xdg-open"))
    assert programs.args("xdg-open") == LINK
    programs.release()


@pytest.mark.parametrize("xdg_mime", [None, "exit 3"])
def test_linux_says_it_cannot_tell_when_xdg_mime_cannot_answer(programs, monkeypatch,
                                                               xdg_mime):
    if xdg_mime is not None:
        programs.make("xdg-mime", xdg_mime)
    programs.make("xdg-open", "exit 0")
    got = answer_of(LINK, "linux", monkeypatch)
    assert got == app_links.UNKNOWN
    assert wait_for(lambda: programs.args("xdg-open") == LINK)


def test_linux_says_no_app_when_xdg_open_is_not_there(programs, monkeypatch):
    programs.make("xdg-mime", "echo amber.desktop")
    got = answer_of(LINK, "linux", monkeypatch)
    assert got == app_links.NO_APP


# -- Windows (a fake registry; not verified on a device) ------------------------

class FakeRegistry:
    """winreg's shape: HKCR keys by path, each a dict of values."""
    HKEY_CLASSES_ROOT = "HKCR"

    def __init__(self, keys, fail=None):
        self.keys = {path.lower(): values for path, values in keys.items()}
        self.fail = fail

    class _Key:
        def __init__(self, values):
            self.values = values

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def OpenKey(self, root, path):  # noqa: N802, winreg's name
        if self.fail is not None:
            raise self.fail
        assert root == self.HKEY_CLASSES_ROOT
        if path.lower() not in self.keys:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return self._Key(self.keys[path.lower()])

    def QueryValueEx(self, key, name):  # noqa: N802, winreg's name
        if name not in key.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return key.values[name], 1


AMBER = {"nostrconnect": {"URL Protocol": "", "": "URL:nostrconnect"},
         r"nostrconnect\shell\open\command": {"": '"C:\\Amber\\amber.exe" "%1"'}}


@pytest.fixture
def startfile(monkeypatch):
    """The shell's open: what it was given; ``error`` it raises; with
    ``held``, it waits until the test sets ``release`` (at most 20 s)."""
    def start(uri):
        started.append(uri)
        if started.held:
            started.release.wait(20)
            started.ended.set()
        if started.error is not None:
            raise started.error

    started = type("Started", (list,), {"error": None, "held": False})()
    started.release, started.ended = threading.Event(), threading.Event()
    monkeypatch.setattr(app_links.os, "startfile", start, raising=False)
    return started


@pytest.mark.parametrize("keys", [
    {},                                                         # nothing registered
    {"nostrconnect": {"": "URL:nostrconnect"},                  # not a URL protocol
     r"nostrconnect\shell\open\command": {"": "amber.exe %1"}},
    {"nostrconnect": {"URL Protocol": ""}},                     # no way to open it
    {"nostrconnect": {"URL Protocol": ""},
     r"nostrconnect\shell\open\command": {"": "  "}},           # an empty command
])
def test_windows_says_no_app_without_a_registration_and_never_asks_the_shell(
        monkeypatch, startfile, keys):
    got = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(keys))
    assert got == app_links.NO_APP and startfile == []


def test_windows_opens_a_registered_scheme_off_the_gui_thread(monkeypatch, startfile):
    # RG-M2r3 8: the shell's open held, the window turns meanwhile.
    startfile.held = True
    got = open_held(monkeypatch, "windows", registry=FakeRegistry(AMBER))
    assert got == [] and not startfile.ended.is_set()
    turns_while(lambda: startfile == [LINK], startfile.ended.is_set)
    assert got == []
    startfile.release.set()
    assert wait_for(lambda: got == [app_links.OPENED])
    assert app_links.windows_registered("nostrconnect", FakeRegistry(AMBER))
    assert app_links.windows_registered("NostrConnect", FakeRegistry(AMBER))


def test_windows_says_no_app_when_the_shell_refuses(monkeypatch, startfile):
    startfile.error = OSError(f"no application is associated with {LINK}")
    got = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(AMBER))
    assert got == app_links.NO_APP and startfile == [LINK]


def test_windows_cannot_tell_when_its_registry_cannot_be_read(monkeypatch, startfile):
    # A Python without winreg: the named case (RG-M2r3 9).
    def no_winreg(name):
        raise ModuleNotFoundError(f"No module named {name!r}")
    monkeypatch.setattr(app_links.importlib, "import_module", no_winreg)
    got = answer_of(LINK, "windows", monkeypatch)
    assert got == app_links.UNKNOWN and startfile == [LINK]


def test_a_programming_error_is_never_swallowed(monkeypatch, startfile):
    # RG-M2r3 9: ``except Exception`` took any bug for "cannot tell".
    monkeypatch.setattr(app_links, "_platform", lambda: "windows")
    with pytest.raises(TypeError):
        app_links.open_link(LINK, lambda _answer: None,
                            registry=FakeRegistry(AMBER, fail=TypeError("a bug")))
    monkeypatch.setattr(app_links, "_platform", lambda: "mac")
    monkeypatch.setattr(app_links, "_open_on_mac",
                        lambda uri, done: (_ for _ in ()).throw(ValueError("a bug")))
    with pytest.raises(ValueError):
        app_links.open_link(LINK, lambda _answer: None)


def test_a_worker_that_raises_answers_it_cannot_tell_and_its_error_goes_on(monkeypatch,
                                                                         startfile):
    raised = []
    monkeypatch.setattr(threading, "excepthook", lambda args: raised.append(args.exc_type))
    startfile.error = ValueError("a bug")
    got = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(AMBER))
    assert got == app_links.UNKNOWN and wait_for(lambda: raised == [ValueError])


def test_no_thread_to_hand_it_over_is_said_as_cannot_tell(monkeypatch, startfile):
    class NoThread:
        def __init__(self, **_kw):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(app_links.threading, "Thread", NoThread)
    got = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(AMBER))
    assert got == app_links.UNKNOWN and startfile == [] and app_links._running == []


# -- only a link, and never in a log --------------------------------------------

@pytest.mark.parametrize("link", ["", "-a nostrconnect://x", "nostrconnect://a b",
                                  "nostrconnect://\u00e4", "no-scheme", "1x://y",
                                  "nostrconnect://x\n--help"])
@pytest.mark.parametrize("platform", ["mac", "linux", "windows"])
def test_only_a_link_is_handed_to_the_system(monkeypatch, link, platform):
    for seam in ("_run", "_start_detached", "_in_worker"):
        monkeypatch.setattr(app_links, seam, lambda *a, **k: pytest.fail("handed over"))
    got = answer_of(link, platform, monkeypatch, registry=FakeRegistry(AMBER))
    assert got == app_links.NO_APP


def test_the_link_and_its_secret_are_never_logged(programs, monkeypatch, startfile, caplog):
    # RG-M2r2b 3: a timeout's trace quoted the command, the link and its
    # secret, into myeditor.log. Every way through, the failing ones too.
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(app_links, "ANSWER_WAIT_MS", 300)
    for body in ("exec /bin/sleep 30", "exit 1", "exit 0"):
        programs.make("open", body)
        answer_of(LINK, "mac", monkeypatch)
    for mime in ("exit 0", "exit 3", "echo amber.desktop"):
        programs.make("xdg-mime", mime)
        answer_of(LINK, "linux", monkeypatch)
    (programs.path / "xdg-open").unlink(missing_ok=True)
    answer_of(LINK, "linux", monkeypatch)
    startfile.error = OSError(f"no application is associated with {LINK}")
    for registry in (FakeRegistry({}), FakeRegistry(AMBER)):
        answer_of(LINK, "windows", monkeypatch, registry=registry)
    monkeypatch.setattr(app_links.importlib, "import_module",
                        lambda name: (_ for _ in ()).throw(ModuleNotFoundError(LINK)))
    answer_of(LINK, "windows", monkeypatch)
    logged = [record.getMessage() + (caplog.handler.format(record) if record.exc_info else "")
              for record in caplog.records]
    assert logged, "nothing was logged, so nothing was checked"
    for text in logged:
        assert SECRET not in text and "ab" * 32 not in text, text
