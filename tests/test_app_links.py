# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""app_links.open_link hands a signer app's link to the system and says
honestly whether an app took it (RG-M2r2b 3 and 4).

What must hold:

  Nothing waits on the GUI thread: the call returns at once and the
  answer comes on a later turn, also when the opening program hangs.
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
            script.write_text(f'#!/bin/sh\necho "$@" > "{folder}/{name}.args"\n{body}\n')
            script.chmod(script.stat().st_mode | stat.S_IEXEC)

        def args(self, name):
            target = folder / f"{name}.args"
            return target.read_text().strip() if target.exists() else None

    # Only the stand-ins: a real opener of the system is never reached.
    monkeypatch.setenv("PATH", str(folder))
    return Programs()


def answer_of(uri, platform, monkeypatch, *, registry=None, within_s=8.0):
    """What open_link answers on ``platform``, and how long the call
    itself took (it must return at once)."""
    monkeypatch.setattr(app_links, "_platform", lambda: platform)
    got = []
    loop = QEventLoop()

    def done(answer):
        got.append(answer)
        loop.quit()
    started = time.monotonic()
    app_links.open_link(uri, done, registry=registry)
    returned_in = time.monotonic() - started
    assert got == [], "answered on the GUI thread, before a later turn"
    if not got:
        # A deadline for a test that would otherwise hang, not a wait the
        # answer depends on.
        QTimer.singleShot(int(within_s * 1000), loop.quit)
        loop.exec()
    assert len(got) == 1, "never answered"
    return got[0], returned_in


def wait_for(check, within_s=5.0):
    deadline = time.monotonic() + within_s
    while not check() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    return check()


def test_a_signer_apps_link_is_not_a_url_qt_takes():
    # Why not QDesktopServices: the key is a 64-character host label, one
    # more than a host label may have.
    assert not QUrl(LINK).isValid()
    assert QUrl(LINK.replace("ab" * 32, "a" * 63)).isValid()


# -- macOS --------------------------------------------------------------------

@pytest.mark.parametrize("code, answer", [(0, app_links.OPENED), (1, app_links.NO_APP)])
def test_macos_answers_by_opens_exit(programs, monkeypatch, code, answer):
    programs.make("open", f"exit {code}")
    got, returned_in = answer_of(LINK, "mac", monkeypatch)
    assert got == answer and returned_in < 0.5
    assert programs.args("open") == LINK


def test_an_opening_that_hangs_never_holds_the_window_and_cannot_tell(programs, monkeypatch):
    programs.make("open", "exec /bin/sleep 30")
    monkeypatch.setattr(app_links, "ANSWER_WAIT_MS", 300)
    got, returned_in = answer_of(LINK, "mac", monkeypatch)
    assert got == app_links.UNKNOWN and returned_in < 0.5
    assert app_links._running == []                     # the child was let go


# -- Linux ----------------------------------------------------------------------

def test_linux_says_no_app_when_none_is_set_up_and_opens_nothing(programs, monkeypatch):
    programs.make("xdg-mime", "exit 0")                 # no default: nothing printed
    programs.make("xdg-open", "exit 0")                 # would hand it to a browser
    got, _ = answer_of(LINK, "linux", monkeypatch)
    assert got == app_links.NO_APP
    assert programs.args("xdg-mime") == "query default x-scheme-handler/nostrconnect"
    assert programs.args("xdg-open") is None


def test_linux_starts_xdg_open_detached_when_an_app_is_set_up(programs, monkeypatch):
    # On a desktop xdg-open does not know (sway, i3), it runs the handler in
    # the foreground: never waited for.
    programs.make("xdg-mime", "echo amber.desktop")
    programs.make("xdg-open", "exec /bin/sleep 3")
    got, returned_in = answer_of(LINK, "linux", monkeypatch, within_s=2.0)
    assert got == app_links.OPENED and returned_in < 0.5
    assert wait_for(lambda: programs.args("xdg-open") == LINK)


@pytest.mark.parametrize("xdg_mime", [None, "exit 3"])
def test_linux_says_it_cannot_tell_when_xdg_mime_cannot_answer(programs, monkeypatch,
                                                               xdg_mime):
    if xdg_mime is not None:
        programs.make("xdg-mime", xdg_mime)
    programs.make("xdg-open", "exit 0")
    got, _ = answer_of(LINK, "linux", monkeypatch)
    assert got == app_links.UNKNOWN
    assert wait_for(lambda: programs.args("xdg-open") == LINK)


def test_linux_says_no_app_when_xdg_open_is_not_there(programs, monkeypatch):
    programs.make("xdg-mime", "echo amber.desktop")
    got, _ = answer_of(LINK, "linux", monkeypatch)
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
    started = []

    def start(uri):
        started.append(uri)
        if started.error is not None:
            raise started.error

    started = type("Started", (list,), {"error": None})()
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
    got, _ = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(keys))
    assert got == app_links.NO_APP and startfile == []


def test_windows_opens_a_registered_scheme_off_the_gui_thread(monkeypatch, startfile):
    got, returned_in = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(AMBER))
    assert got == app_links.OPENED and startfile == [LINK] and returned_in < 0.5
    assert app_links.windows_registered("nostrconnect", FakeRegistry(AMBER))
    assert app_links.windows_registered("NostrConnect", FakeRegistry(AMBER))


def test_windows_says_no_app_when_the_shell_refuses(monkeypatch, startfile):
    startfile.error = OSError(f"no application is associated with {LINK}")
    got, _ = answer_of(LINK, "windows", monkeypatch, registry=FakeRegistry(AMBER))
    assert got == app_links.NO_APP and startfile == [LINK]


def test_windows_cannot_tell_when_its_registry_cannot_be_read(monkeypatch, startfile):
    registry = FakeRegistry(AMBER, fail=RuntimeError("the registry is not there"))
    got, _ = answer_of(LINK, "windows", monkeypatch, registry=registry)
    assert got == app_links.UNKNOWN and startfile == [LINK]


# -- only a link, and never in a log --------------------------------------------

@pytest.mark.parametrize("link", ["", "-a nostrconnect://x", "nostrconnect://a b",
                                  "nostrconnect://\u00e4", "no-scheme", "1x://y",
                                  "nostrconnect://x\n--help"])
@pytest.mark.parametrize("platform", ["mac", "linux", "windows"])
def test_only_a_link_is_handed_to_the_system(monkeypatch, link, platform):
    for seam in ("_run", "_start_detached", "_in_worker"):
        monkeypatch.setattr(app_links, seam, lambda *a, **k: pytest.fail("handed over"))
    got, _ = answer_of(link, platform, monkeypatch, registry=FakeRegistry(AMBER))
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
    for registry in (FakeRegistry({}), FakeRegistry(AMBER),
                     FakeRegistry(AMBER, fail=RuntimeError(f"broken at {LINK}"))):
        answer_of(LINK, "windows", monkeypatch, registry=registry)
    monkeypatch.setattr(app_links, "_open_on_mac",
                        lambda uri, done: (_ for _ in ()).throw(ValueError(uri)))
    answer_of(LINK, "mac", monkeypatch)
    logged = [record.getMessage() + (caplog.handler.format(record) if record.exc_info else "")
              for record in caplog.records]
    assert logged, "nothing was logged, so nothing was checked"
    for text in logged:
        assert SECRET not in text and "ab" * 32 not in text, text
