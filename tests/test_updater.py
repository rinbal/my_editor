# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins how the updater checks, prepares and swaps in a new version.

What must hold:

  Only a file whose SHA-256 matches the one GitHub published is installed.
  A mismatch, or a release that names no hash, installs nothing.

  A Mac app is only replaced where it can be: not from a translocated or
  read-only location. The swap puts the old app back if the new one cannot
  be moved in, and opens whichever is there.

  A .deb is installed while the window is still open, so a closed password
  prompt is "nothing changed", not an error. Nothing the updater runs can
  wait for typed input, and apt-get never removes other packages.

  The helper scripts quote every path: a folder with spaces or quotes in
  its name must not break, or change, what runs. A swap that fails leaves
  the old version in place and opens it, and leaves no half-copied app or
  stray download behind.

No network is used: replies are fakes. The helper scripts really run (on
POSIX), in temporary folders, with the system tools they call (hdiutil,
ditto, codesign, open) replaced by stand-ins on PATH.
"""

import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtNetwork import QNetworkReply  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import updater  # noqa: E402
from update_check import parse_sha256  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


class FakeReply:
    """Just enough of QNetworkReply for UpdateInstaller._on_finished."""

    def __init__(self, data: bytes, error=QNetworkReply.NetworkError.NoError):
        self._data = data
        self._error = error

    def error(self):
        return self._error

    def errorString(self):
        return "network down"

    def readAll(self):
        data, self._data = self._data, b""
        return data

    def deleteLater(self):
        pass

    def abort(self):
        pass


def download(tmp_path, payload: bytes, sha256: str, kind=updater.WINDOWS_INSTALLER,
             reply=None):
    """Run a download to completion with a fake reply; return (signals, dest)."""
    installer = updater.UpdateInstaller(kind)
    seen = {"ready": [], "failed": []}
    installer.ready.connect(lambda p: seen["ready"].append(p))
    installer.failed.connect(lambda m, retry: seen["failed"].append((m, retry)))
    dest = tmp_path / "setup.exe"
    installer._dest = str(dest)
    installer._fh = open(dest, "wb")
    installer._expected_size = len(payload)
    installer._expected_sha256 = sha256
    installer._hash = hashlib.sha256()
    installer._reply = reply or FakeReply(payload)
    installer._on_finished()
    return seen, dest


# -- the published hash ---------------------------------------------------------

def test_github_digests_are_read_as_lowercase_hex():
    hex_ = "AB" * 32
    assert parse_sha256(f"sha256:{hex_}") == hex_.lower()
    for bad in (None, "", "sha256:xyz", "md5:" + "a" * 32, "sha256:" + "a" * 63, 42):
        assert parse_sha256(bad) == ""


def test_a_download_that_matches_its_hash_is_ready(tmp_path):
    payload = b"installer bytes"
    seen, dest = download(tmp_path, payload, hashlib.sha256(payload).hexdigest())
    assert seen == {"ready": [str(dest)], "failed": []}
    assert dest.exists()


def test_a_download_that_does_not_match_is_deleted_and_never_ready(tmp_path):
    seen, dest = download(tmp_path, b"tampered bytes", hashlib.sha256(b"original").hexdigest())
    assert seen["ready"] == []
    # Another download can arrive intact, so trying again can help.
    assert seen["failed"] == [("The download was damaged or changed on the way, "
                               "so it wasn't installed.", True)]
    assert not dest.exists()


def test_a_network_error_is_one_sentence_with_its_reason(tmp_path):
    reply = FakeReply(b"", error=QNetworkReply.NetworkError.ConnectionRefusedError)
    seen, dest = download(tmp_path, b"x", "0" * 64, reply=reply)
    assert seen["failed"] == [("The download didn't finish (network down).", True)]
    assert not dest.exists()


def test_a_network_reason_that_names_the_download_url_is_left_out():
    assert updater.download_failure("Connection refused.") == \
        "The download didn't finish (Connection refused)."
    assert updater.download_failure(
        "Error transferring https://objects.example/x?sig=abc - server replied: Not Found"
    ) == "The download didn't finish."
    assert updater.download_failure("") == "The download didn't finish."


def test_a_release_without_a_hash_is_never_downloaded():
    installer = updater.UpdateInstaller(updater.WINDOWS_INSTALLER)
    failed = []
    installer.failed.connect(lambda m, retry: failed.append((m, retry)))
    installer.start(SimpleNamespace(name="setup.exe", url="https://x", size=1, sha256=""))
    # The same release would be just as unverifiable on a second try.
    assert failed == [("MyEditor couldn't check this download, so it wasn't installed.",
                       False)]
    assert installer._reply is None


# -- where a Mac app can be replaced ---------------------------------------------

def make_bundle(root, name="MyEditor.app"):
    exe = root / name / "Contents" / "MacOS" / "my-editor"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    return str(exe)


def test_the_bundle_is_found_from_the_executable(tmp_path):
    exe = make_bundle(tmp_path)
    assert updater.mac_bundle_path(exe) == os.path.realpath(str(tmp_path / "MyEditor.app"))
    assert updater.mac_bundle_path(str(tmp_path / "python3")) is None


def test_a_writable_bundle_can_update_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    assert updater._mac_bundle_replaceable(make_bundle(tmp_path))


def test_a_translocated_app_cannot_update_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    root = tmp_path / "AppTranslocation" / "ABCD" / "d"
    root.mkdir(parents=True)
    assert not updater._mac_bundle_replaceable(make_bundle(root))


def test_a_read_only_folder_cannot_be_updated_in_place(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    exe = make_bundle(tmp_path)
    monkeypatch.setattr(updater.os, "access", lambda path, mode: False)
    assert not updater._mac_bundle_replaceable(exe)


def test_missing_system_tools_mean_no_in_place_update(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda tool: None)
    assert not updater._mac_bundle_replaceable(make_bundle(tmp_path))


def test_the_staging_copy_sits_hidden_beside_the_app():
    assert updater.mac_staging_path("/Applications/MyEditor.app") == \
        "/Applications/.MyEditor.app.update"


# -- the scripts, run for real --------------------------------------------------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="the helper scripts are POSIX shell")

# Spaces, quotes and an ampersand: quoting mistakes break here, or run something else.
AWKWARD = "Jo O'Neil's \"Apps\" & Tools"


def dead_pid() -> int:
    """The id of a process that has already exited, so the helper's wait ends at once."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def stub(bin_dir, name: str, body: str) -> None:
    """A stand-in for a system tool, found first on PATH."""
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    path.chmod(0o755)


def run_script(script: str, bin_dir) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return subprocess.run(["sh", "-c", script], env=env, capture_output=True,
                          text=True, timeout=60)


@pytest.fixture
def tools(tmp_path):
    """A folder of stand-in tools and the log they write to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "tools.log"
    log.write_text("")
    # `open` only records what it was asked to open.
    stub(bin_dir, "open", f'printf "%s\\n" "$@" >> {shlex.quote(str(log))}\n')
    return SimpleNamespace(bin=bin_dir, log=log)


def fail_mv_for(bin_dir, pattern: str) -> None:
    """`mv` refuses any move involving a path that matches ``pattern``."""
    real_mv = shutil.which("mv")
    stub(bin_dir, "mv", f"""\
        for arg in "$@"; do
          case "$arg" in {pattern}) exit 1;; esac
        done
        exec {shlex.quote(real_mv)} "$@"
        """)


def make_app(folder, version: str) -> None:
    (folder / "Contents" / "MacOS").mkdir(parents=True)
    (folder / "Contents" / "version").write_text(version)


def app_version(folder) -> str:
    return (folder / "Contents" / "version").read_text().strip()


@pytest.fixture
def mac_install(tmp_path):
    apps = tmp_path / AWKWARD
    apps.mkdir()
    bundle = apps / "MyEditor.app"
    make_app(bundle, "old")
    staged = apps / ".MyEditor.app.update"
    assert str(staged) == updater.mac_staging_path(str(bundle))
    return SimpleNamespace(apps=apps, bundle=bundle, staged=staged)


@posix_only
def test_the_mac_swap_puts_the_new_app_in_place_and_opens_it(tools, mac_install):
    make_app(mac_install.staged, "new")
    script = updater.mac_swap_script(dead_pid(), str(mac_install.staged), str(mac_install.bundle))
    assert run_script(script, tools.bin).returncode == 0
    assert app_version(mac_install.bundle) == "new"
    assert sorted(os.listdir(mac_install.apps)) == ["MyEditor.app"]   # no copy left over
    assert tools.log.read_text().splitlines() == [str(mac_install.bundle)]


@posix_only
def test_a_mac_swap_that_cannot_move_the_new_app_in_puts_the_old_one_back(tools, mac_install):
    make_app(mac_install.staged, "new")
    fail_mv_for(tools.bin, "*.update")
    script = updater.mac_swap_script(dead_pid(), str(mac_install.staged), str(mac_install.bundle))
    run_script(script, tools.bin)
    assert app_version(mac_install.bundle) == "old"
    assert sorted(os.listdir(mac_install.apps)) == ["MyEditor.app"]
    assert tools.log.read_text().splitlines() == [str(mac_install.bundle)]


@posix_only
def test_a_mac_swap_that_cannot_move_the_old_app_out_opens_it_unchanged(tools, mac_install):
    make_app(mac_install.staged, "new")
    fail_mv_for(tools.bin, "*.previous")
    script = updater.mac_swap_script(dead_pid(), str(mac_install.staged), str(mac_install.bundle))
    run_script(script, tools.bin)
    assert app_version(mac_install.bundle) == "old"
    assert sorted(os.listdir(mac_install.apps)) == ["MyEditor.app"]
    assert tools.log.read_text().splitlines() == [str(mac_install.bundle)]


def make_appimage(path, version: str, ran) -> None:
    path.write_text(f"#!/bin/sh\necho {version} > {shlex.quote(str(ran))}\n")


@pytest.fixture
def appimage_install(tmp_path):
    folder = tmp_path / AWKWARD
    folder.mkdir()
    ran = tmp_path / "ran.txt"
    app = folder / "MyEditor.AppImage"
    make_appimage(app, "old", ran)
    app.chmod(0o755)
    new = folder / "MyEditor.AppImage.new"
    make_appimage(new, "new", ran)
    new.chmod(0o644)   # the swap is what makes it executable
    return SimpleNamespace(folder=folder, app=app, new=new, ran=ran)


@posix_only
def test_the_appimage_swap_moves_the_new_file_in_and_opens_it(tools, appimage_install):
    a = appimage_install
    run_script(updater.appimage_swap_script(dead_pid(), str(a.new), str(a.app)), tools.bin)
    assert a.ran.read_text().strip() == "new"
    assert os.listdir(a.folder) == ["MyEditor.AppImage"]
    assert os.stat(a.app).st_mode & stat.S_IXUSR


@posix_only
def test_an_appimage_swap_that_fails_opens_the_old_one_and_removes_the_new_file(
        tools, appimage_install):
    a = appimage_install
    fail_mv_for(tools.bin, "*.new")
    run_script(updater.appimage_swap_script(dead_pid(), str(a.new), str(a.app)), tools.bin)
    assert a.ran.read_text().strip() == "old"
    assert os.listdir(a.folder) == ["MyEditor.AppImage"]


@posix_only
def test_the_relaunch_helper_waits_for_this_process_then_opens_the_app(tools, tmp_path):
    folder = tmp_path / AWKWARD
    folder.mkdir()
    ran = tmp_path / "ran.txt"
    app = folder / "my-editor"
    make_appimage(app, "relaunched", ran)
    app.chmod(0o755)
    # A process that is still running, and not this test's child: once it
    # exits, the system reaps it, as it does MyEditor's.
    sleeper = subprocess.run(["sh", "-c", "sleep 1 >/dev/null 2>&1 & echo $!"],
                             capture_output=True, text=True, check=True)
    pid = int(sleeper.stdout.strip())
    started = time.monotonic()
    run_script(updater.relaunch_script(pid, str(app)), tools.bin)
    assert time.monotonic() - started > 0.5
    assert ran.read_text().strip() == "relaunched"


def stage_tools(tools, *, image_has_app=True, copy_works=True, signature_ok=True):
    """Stand-ins for hdiutil, ditto and codesign. The fake image holds a
    MyEditor.app when ``image_has_app``; every attach and detach is logged."""
    log = shlex.quote(str(tools.log))
    make = ('mkdir -p "$mnt/MyEditor.app/Contents/MacOS"; '
            'echo new > "$mnt/MyEditor.app/Contents/version"') if image_has_app else ":"
    stub(tools.bin, "hdiutil", f"""\
        case "$1" in
          attach)
            while [ $# -gt 0 ]; do
              if [ "$1" = "-mountpoint" ]; then mnt="$2"; fi
              shift
            done
            echo "attach $mnt" >> {log}
            {make}
            ;;
          detach)
            echo "detach $2" >> {log}
            rm -rf "$2/MyEditor.app"
            ;;
        esac
        """)
    if copy_works:
        stub(tools.bin, "ditto", 'exec cp -R "$1" "$2"\n')
    else:
        # Fails halfway, the way a full disk does: part of the app is there.
        stub(tools.bin, "ditto", 'mkdir -p "$2/Contents"; echo partial > "$2/Contents/x"; exit 1\n')
    stub(tools.bin, "codesign", "exit 0\n" if signature_ok else "exit 1\n")


def stage(tools, mac_install, tmp_path):
    dmg = tmp_path / "My Editor 3.4.dmg"
    dmg.write_bytes(b"image")
    result = run_script(updater.mac_stage_script(str(dmg), str(mac_install.staged)), tools.bin)
    mounts = [line.split(" ", 1)[1] for line in tools.log.read_text().splitlines()]
    return result.returncode, mounts


@posix_only
def test_staging_copies_the_new_app_beside_the_old_one_and_closes_the_image(
        tools, mac_install, tmp_path):
    stage_tools(tools)
    code, mounts = stage(tools, mac_install, tmp_path)
    assert code == 0
    assert app_version(mac_install.staged) == "new"
    assert app_version(mac_install.bundle) == "old"
    attached, detached = mounts
    assert attached == detached and not os.path.exists(attached)


@pytest.mark.parametrize("failure, code", [
    ({"image_has_app": False}, 12),
    ({"copy_works": False}, 13),
    ({"signature_ok": False}, 14),
])
@posix_only
def test_a_failed_staging_leaves_no_copy_and_no_open_image(
        tools, mac_install, tmp_path, failure, code):
    stage_tools(tools, **failure)
    result, mounts = stage(tools, mac_install, tmp_path)
    assert result == code and code in updater._MAC_STAGE_ERRORS
    assert not mac_install.staged.exists()   # no half-copied app beside the real one
    assert sorted(os.listdir(mac_install.apps)) == ["MyEditor.app"]
    assert app_version(mac_install.bundle) == "old"
    attached, detached = mounts
    assert attached == detached and not os.path.exists(attached)


@posix_only
def test_an_image_that_cannot_be_opened_is_reported(tools, mac_install, tmp_path):
    stub(tools.bin, "hdiutil", "exit 1\n")
    result, _ = stage(tools, mac_install, tmp_path)
    assert result == 11
    assert not mac_install.staged.exists()


@posix_only
def test_an_installer_that_goes_away_after_a_system_step_leaves_memory_intact():
    # The dialog that owns the installer can close right after a step ends.
    # The finished process used to delete itself later, and if the installer
    # went first it was destroyed twice. Run in a child: corrupted memory
    # aborts the whole process.
    script = textwrap.dedent("""\
        import gc, os, sys
        sys.path.insert(0, sys.argv[1])
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QTextEdit
        app = QApplication(sys.argv[:1])
        import updater
        installer = updater.UpdateInstaller(updater.DEB)
        codes = []
        installer._run("true", [], codes.append)
        assert installer._process.waitForFinished(10000)
        app.processEvents()
        del installer
        gc.collect()
        for _ in range(10):
            w = QTextEdit()
            w.setPlainText("x\\n" * 300)
            w.show()
            QTest.qWait(5)
            w.close()
        print("ok", codes)
        """)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = subprocess.run([sys.executable, "-c", script, repo], capture_output=True,
                          text=True, timeout=120,
                          env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "ok [0]" in proc.stdout


@posix_only
def test_a_system_step_never_waits_for_typed_input(qt_app):
    # `cat` reads its input until it ends. With nothing attached it would
    # wait forever, the way apt-get waits on a question nobody sees.
    installer = updater.UpdateInstaller(updater.DEB)
    codes = []
    installer._run("cat", [], codes.append)
    assert installer._process.waitForFinished(10_000)
    qt_app.processEvents()
    assert codes == [0]


# -- preparing ------------------------------------------------------------------------

def prepare_with_exit_code(kind, code, tmp_path, monkeypatch, executable=None):
    installer = updater.UpdateInstaller(kind, executable=executable)
    calls = []

    def fake_run(program, args, on_exit):
        calls.append([program, *args])
        on_exit(code)

    monkeypatch.setattr(installer, "_run", fake_run)
    seen = {"prepared": [], "declined": [], "failed": []}
    installer.prepared.connect(seen["prepared"].append)
    installer.declined.connect(seen["declined"].append)
    installer.failed.connect(lambda m, retry: seen["failed"].append((m, retry)))
    downloaded = tmp_path / "update.bin"
    downloaded.write_bytes(b"x")
    installer.prepare(str(downloaded))
    return seen, calls, downloaded


def test_windows_and_appimage_have_nothing_to_prepare(tmp_path, monkeypatch):
    for kind in (updater.WINDOWS_INSTALLER, updater.APPIMAGE):
        seen, calls, path = prepare_with_exit_code(kind, 0, tmp_path, monkeypatch)
        assert seen["prepared"] == [str(path)] and calls == []


def test_the_deb_is_installed_with_a_password_prompt(tmp_path, monkeypatch):
    seen, calls, path = prepare_with_exit_code(
        updater.DEB, 0, tmp_path, monkeypatch, executable="/opt/my-editor/my-editor")
    assert calls == [["pkexec", "apt-get", "install", "-y", "--no-remove", str(path)]]
    assert seen["prepared"] == [os.path.realpath("/opt/my-editor/my-editor")]
    assert not path.exists()   # the package file is not left in the temp folder


def test_a_closed_password_prompt_is_declined_not_failed(tmp_path, monkeypatch):
    seen, _, _ = prepare_with_exit_code(updater.DEB, 126, tmp_path, monkeypatch)
    assert seen["failed"] == [] and "Nothing was changed" in seen["declined"][0]


def test_permission_not_granted_is_said_once_without_naming_the_next_step(
        tmp_path, monkeypatch):
    # pkexec(1): 127 means not authorized, authentication failed, or no
    # prompt could be shown. The dialog adds what to do next.
    seen, _, _ = prepare_with_exit_code(updater.DEB, 127, tmp_path, monkeypatch)
    assert seen["failed"] == [("MyEditor didn't get permission to install the update.", True)]


def test_a_failed_package_install_does_not_name_the_next_step_either(tmp_path, monkeypatch):
    seen, _, _ = prepare_with_exit_code(updater.DEB, 100, tmp_path, monkeypatch)
    (message, retryable), = seen["failed"]
    assert retryable and "Try again" not in message and "guide" not in message


def test_a_mac_staging_failure_is_explained(tmp_path, monkeypatch):
    exe = make_bundle(tmp_path)
    seen, calls, _ = prepare_with_exit_code(updater.MACOS_APP, 14, tmp_path, monkeypatch,
                                            executable=exe)
    assert calls[0][:2] == ["sh", "-c"]
    # The download was checked, so a second try fetches the same app.
    assert seen["failed"] == [("The new version didn't pass the macOS integrity check, "
                               "so it wasn't installed.", False)]


def test_a_full_disk_while_staging_can_be_tried_again(tmp_path, monkeypatch):
    exe = make_bundle(tmp_path)
    seen, _, _ = prepare_with_exit_code(updater.MACOS_APP, 13, tmp_path, monkeypatch,
                                        executable=exe)
    assert seen["failed"][0][1] is True


def test_a_staged_mac_app_is_what_gets_applied(tmp_path, monkeypatch):
    exe = make_bundle(tmp_path)
    seen, _, _ = prepare_with_exit_code(updater.MACOS_APP, 0, tmp_path, monkeypatch,
                                        executable=exe)
    bundle = updater.mac_bundle_path(exe)
    assert seen["prepared"] == [updater.mac_staging_path(bundle)]


def test_calling_off_the_restart_removes_what_was_prepared(tmp_path):
    staged = tmp_path / ".MyEditor.app.update"
    (staged / "Contents").mkdir(parents=True)
    updater.UpdateInstaller(updater.MACOS_APP).discard_prepared(str(staged))
    assert not staged.exists()

    setup = tmp_path / "setup.exe"
    setup.write_bytes(b"x")
    updater.UpdateInstaller(updater.WINDOWS_INSTALLER).discard_prepared(str(setup))
    assert not setup.exists()

    # Never remove a folder that is not a staging copy.
    other = tmp_path / "MyEditor.app"
    other.mkdir()
    updater.UpdateInstaller(updater.MACOS_APP).discard_prepared(str(other))
    assert other.exists()


def test_only_the_deb_is_installed_before_the_restart():
    kinds = (updater.WINDOWS_INSTALLER, updater.APPIMAGE, updater.MACOS_APP, updater.DEB)
    assert [updater.UpdateInstaller(k).installs_before_restart for k in kinds] == \
        [False, False, False, True]


def test_a_download_called_off_before_preparing_is_deleted_with_its_folder():
    asset = SimpleNamespace(name="my-editor-3.4-macos-arm64.dmg")
    dest = updater._download_destination(updater.MACOS_APP, asset)
    with open(dest, "wb") as f:
        f.write(b"x")
    updater.UpdateInstaller(updater.MACOS_APP).discard_download(dest)
    assert not os.path.exists(os.path.dirname(dest))


def test_messages_are_sentences_without_em_dashes():
    texts = [m for m, _ in updater._MAC_STAGE_ERRORS.values()]
    texts += [updater._MAC_STAGE_FAILED[0], updater._CANNOT_SAVE,
              updater.download_failure("x")]
    for text in texts:
        assert "\u2014" not in text
        assert text.endswith(".") and text[0].isupper()


def test_a_swap_that_cannot_start_says_so_in_a_sentence(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    with pytest.raises(RuntimeError, match=r"\AMyEditor couldn't find its AppImage\.\Z"):
        updater.UpdateInstaller(updater.APPIMAGE).apply("/tmp/x.new")
    with pytest.raises(RuntimeError, match=r"\AThis copy of MyEditor can't update itself\.\Z"):
        updater.UpdateInstaller(updater.SOURCE).apply("/tmp/x")


@posix_only   # the symlink case needs POSIX
def test_download_folders_left_behind_are_swept_once_they_are_old(tmp_path):
    # The Windows installer runs from its folder after MyEditor quits, so
    # nothing deletes it then; a later launch does.
    now = time.time()
    day = 24 * 60 * 60

    def folder(name, age):
        path = tmp_path / name
        path.mkdir()
        (path / "my-editor-3.4-windows-setup.exe").write_bytes(b"x")
        os.utime(path, (now - age, now - age))
        return path

    old = folder("my-editor-update-old", 2 * day)
    fresh = folder("my-editor-update-fresh", 60)        # maybe another window's update
    other = folder("someone-elses-folder", 2 * day)
    target = folder("outside", 2 * day)
    link = tmp_path / "my-editor-update-link"
    link.symlink_to(target)
    os.utime(link, (now - 2 * day, now - 2 * day), follow_symlinks=False)

    updater.sweep_stale_downloads(str(tmp_path), now=now)

    assert not old.exists()
    assert fresh.exists() and other.exists()
    assert link.is_symlink() and (target / "my-editor-3.4-windows-setup.exe").exists()


def test_sweeping_a_missing_temp_folder_is_harmless(tmp_path):
    updater.sweep_stale_downloads(str(tmp_path / "missing"))


def test_downloads_land_in_a_private_folder_that_goes_away_with_them():
    asset = SimpleNamespace(name="my-editor_3.4_amd64.deb")
    dest = updater._download_destination(updater.DEB, asset)
    folder = os.path.dirname(dest)
    try:
        assert os.path.basename(dest) == "my-editor_3.4_amd64.deb"
        assert os.path.basename(folder).startswith("my-editor-update-")
        if os.name == "posix":
            assert os.stat(folder).st_mode & 0o077 == 0   # nobody else can enter
        with open(dest, "wb") as f:
            f.write(b"x")
        updater._discard(dest)
        assert not os.path.exists(folder)
    finally:
        if os.path.isdir(folder):
            os.rmdir(folder)
