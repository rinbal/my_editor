#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-app updater for the packaged builds that can safely replace themselves.

Every update runs the same three stages, and only the last happens after
MyEditor has closed:

    download  fetch the release file and check it against the SHA-256
              GitHub published for it. A file that does not match is
              deleted, never run.
    prepare   everything that can fail while the window is still open, so
              a failure is reported where the person can see it. It runs
              after the window has written its tabs down, the last point
              at which the person can call the update off:
                Windows   nothing to do; the installer runs at restart.
                AppImage  nothing to do; the file is already beside the old one.
                macOS     open the disk image, copy the new app next to the
                          running one, and check its code signature.
                .deb      install the package (the system asks for a password).
    apply     start a small helper that waits for MyEditor to quit, puts the
              new version in place, and opens it again.

Install kinds that cannot be replaced safely (a read-only location, a Mac
app run from the disk image, a source checkout) report False from
supports_in_app_update(), and the update dialog walks the person through
the install guide's steps instead (see update_flow.py).
"""

import hashlib
import os
import platform
import shlex
import shutil
import stat
import sys
import tempfile
import time

from PySide6.QtCore import QObject, QProcess, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from release_assets import WINDOWS, appimage_key, asset_key, deb_key, mac_key

# Install kinds.
WINDOWS_INSTALLER = "windows_installer"
APPIMAGE = "appimage"
MACOS_APP = "macos_app"
DEB = "deb"                   # installed by the .deb into /opt/my-editor
LINUX_OTHER = "linux_other"   # a bare onedir folder
SOURCE = "source"

# Where packaging/linux/build_deb.sh puts the PyInstaller folder.
_DEB_PREFIX = "/opt/my-editor/"

# A failure is reported as (message, retryable): complete sentences the
# dialog shows as they are, and whether trying again can help. The dialog
# adds the next step itself ("Try again" only when it can help), so no
# message here names one.

# Exit codes of the macOS staging script, and what each one tells the person.
# The download was checked against its hash, so a retry fetches the very
# same file: an image without the app, or an app that fails the signature
# check, fails again.
_MAC_STAGE_ERRORS = {
    11: ("The downloaded disk image couldn't be opened.", True),
    12: ("The disk image doesn't contain MyEditor.", False),
    13: ("MyEditor couldn't copy the new version. Check that your disk has free space.", True),
    14: ("The new version didn't pass the macOS integrity check, so it wasn't installed.", False),
}
_MAC_STAGE_FAILED = ("MyEditor couldn't prepare the new version.", True)

# pkexec's own exit codes (pkexec(1)): 126 means the person dismissed the
# password prompt; 127 means permission was not granted, because the account
# is not allowed, the password was wrong, or no prompt could be shown.
_PKEXEC_DISMISSED = 126
_PKEXEC_NOT_AUTHORIZED = 127


def detect_install_kind() -> str:
    """Work out how the running app was installed."""
    # The AppImage runtime sets $APPIMAGE to the .AppImage path, even though the
    # payload inside is itself a frozen PyInstaller build, so check it first.
    if os.environ.get("APPIMAGE"):
        return APPIMAGE
    if not getattr(sys, "frozen", False):
        return SOURCE
    if sys.platform == "win32":
        return WINDOWS_INSTALLER
    if sys.platform == "darwin":
        return MACOS_APP
    if os.path.realpath(sys.executable).startswith(_DEB_PREFIX):
        return DEB
    return LINUX_OTHER


def supports_in_app_update(kind: str = None) -> bool:
    """True only for the installs we can replace in place (see module docstring)."""
    kind = kind or detect_install_kind()
    if kind == WINDOWS_INSTALLER:
        return True
    if kind == APPIMAGE:
        return _appimage_writable()
    if kind == MACOS_APP:
        return _mac_bundle_replaceable()
    if kind == DEB:
        return _deb_installable()
    return False


def install_asset_key(kind: str, machine: str = None):
    """The release_assets key of the file that updates this install, or None.

    The key carries the CPU arch, so a wrong-arch build is never swapped in
    (that would replace the app with one that cannot run).
    """
    machine = machine or platform.machine()
    if kind == WINDOWS_INSTALLER:
        return WINDOWS
    if kind == APPIMAGE:
        return appimage_key(machine)
    if kind == MACOS_APP:
        return mac_key(machine)
    if kind == DEB:
        return deb_key(machine)
    return None


def select_asset(kind: str, assets, machine: str = None):
    """Pick the release asset that matches this install, or None if there is none."""
    wanted = install_asset_key(kind, machine)
    if wanted is None:
        return None
    return _first(assets, lambda a: asset_key(a.name) == wanted)


def _first(items, predicate):
    for item in items:
        if predicate(item):
            return item
    return None


def _appimage_writable() -> bool:
    path = os.environ.get("APPIMAGE")
    return bool(path) and os.access(os.path.dirname(path) or ".", os.W_OK)


def mac_bundle_path(executable: str = None):
    """The .app folder the running executable lives in, or None.

    A bundle runs from ``<Name>.app/Contents/MacOS/<binary>``.
    """
    exe = os.path.realpath(executable or sys.executable)
    bundle = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
    return bundle if bundle.endswith(".app") else None


def _mac_bundle_replaceable(executable: str = None) -> bool:
    """Whether the running app sits somewhere it can be replaced.

    Not when macOS runs it from a randomized read-only copy (App
    Translocation: the app was opened where it was downloaded, never
    moved), not from the disk image itself, and not from a folder this
    account cannot write to.
    """
    bundle = mac_bundle_path(executable)
    if bundle is None or "/AppTranslocation/" in bundle:
        return False
    if not all(shutil.which(tool) for tool in ("hdiutil", "ditto", "codesign")):
        return False
    return os.access(os.path.dirname(bundle), os.W_OK) and os.access(bundle, os.W_OK)


def _deb_installable() -> bool:
    """The system can ask for a password and install a package: pkexec, apt,
    and a graphical session for the password prompt to appear in."""
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return has_display and bool(shutil.which("pkexec")) and bool(shutil.which("apt-get"))


def mac_staging_path(bundle: str) -> str:
    """Where the new app is copied before the swap: hidden, and beside the
    old one, so the final move is a rename on the same disk."""
    parent, name = os.path.split(bundle)
    return os.path.join(parent, f".{name}.update")


class UpdateInstaller(QObject):
    """Downloads a release asset, checks it, prepares it, and applies it."""

    progress = Signal(int)     # 0..100 percent of the download
    ready = Signal(str)        # local path of the downloaded, verified file
    prepared = Signal(str)     # what apply() needs; the window may close after apply
    declined = Signal(str)     # the person backed out of a system prompt; nothing changed
    failed = Signal(str, bool)  # what went wrong, and whether trying again can help

    def __init__(self, kind: str, parent=None, *, executable: str = None):
        super().__init__(parent)
        self._kind = kind
        self._executable = executable or sys.executable
        self._manager = QNetworkAccessManager(self)
        self._reply = None
        self._fh = None
        self._dest = None
        self._expected_size = 0
        self._expected_sha256 = ""
        self._hash = None
        self._canceled = False
        self._error = None
        self._process = None

    # -- download -----------------------------------------------------------
    def start(self, asset):
        self._canceled = False
        self._error = None
        self._expected_sha256 = (getattr(asset, "sha256", "") or "").lower()
        if not self._expected_sha256:
            # Never install what cannot be checked. update_flow only offers an
            # automatic update when the release names a hash, so this is a guard.
            self.failed.emit("MyEditor couldn't check this download, so it wasn't "
                             "installed.", False)
            return
        try:
            self._dest = _download_destination(self._kind, asset)
            self._fh = open(self._dest, "wb")
        except OSError:
            self.failed.emit(_CANNOT_SAVE, True)
            return
        self._expected_size = asset.size or 0
        self._hash = hashlib.sha256()

        request = QNetworkRequest(QUrl(asset.url))
        # GitHub download URLs redirect to a storage host; follow that.
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        self._reply = self._manager.get(request)
        self._reply.downloadProgress.connect(self._on_progress)
        self._reply.readyRead.connect(self._on_ready_read)
        self._reply.finished.connect(self._on_finished)

    def cancel(self):
        self._canceled = True
        if self._reply is not None:
            self._reply.abort()

    # -- prepare ------------------------------------------------------------
    def prepare(self, path: str):
        """Do whatever can fail before the window closes; emits ``prepared``,
        ``declined`` or ``failed``."""
        if self._kind == MACOS_APP:
            self._stage_mac(path)
        elif self._kind == DEB:
            self._install_deb(path)
        else:
            self.prepared.emit(path)

    def _stage_mac(self, dmg_path: str):
        bundle = mac_bundle_path(self._executable)
        if bundle is None:
            self.failed.emit("MyEditor couldn't find where it is installed.", False)
            return
        script = mac_stage_script(dmg_path, mac_staging_path(bundle))

        def done(code: int):
            _discard(dmg_path)
            if code == 0:
                self.prepared.emit(mac_staging_path(bundle))
            else:
                self.failed.emit(*_MAC_STAGE_ERRORS.get(code, _MAC_STAGE_FAILED))

        self._run("sh", ["-c", script], done)

    def _install_deb(self, deb_path: str):
        def done(code: int):
            _discard(deb_path)
            if code == 0:
                self.prepared.emit(os.path.realpath(self._executable))
            elif code == _PKEXEC_DISMISSED:
                self.declined.emit("The update wasn't installed because the password "
                                   "prompt was closed. Nothing was changed.")
            elif code == _PKEXEC_NOT_AUTHORIZED:
                self.failed.emit("MyEditor didn't get permission to install the update.",
                                 True)
            else:
                self.failed.emit("The package couldn't be installed. Another "
                                 "installation may be running.", True)

        # --no-remove: an update that would take other packages off the
        # system is refused instead of carried out unseen.
        self._run("pkexec", ["apt-get", "install", "-y", "--no-remove",
                             os.path.abspath(deb_path)], done)

    def _run(self, program: str, args, on_exit):
        # The process stays a child of the installer and goes with it. It
        # used to delete itself when it finished (deleteLater), and when the
        # installer went first, before that deferred delete ran, the process
        # was destroyed twice and memory was corrupted.
        process = QProcess(self)
        self._process = process
        # Nobody can type into these processes, so none may wait for input:
        # apt-get and hdiutil read an empty input and go on.
        process.setStandardInputFile(QProcess.nullDevice())

        def finished(code, status):
            self._process = None
            crashed = status != QProcess.ExitStatus.NormalExit
            on_exit(-1 if crashed else code)

        def error(err):
            if err == QProcess.ProcessError.FailedToStart:
                self._process = None
                on_exit(-1)

        process.finished.connect(finished)
        process.errorOccurred.connect(error)
        process.start(program, list(args))

    # -- apply --------------------------------------------------------------
    def apply(self, path: str):
        """Launch the swap. The caller must quit the app right after this.

        Raises RuntimeError, worded for the person, when the swap can't start.
        """
        if self._kind == WINDOWS_INSTALLER:
            _apply_windows(path)
        elif self._kind == APPIMAGE:
            _apply_appimage(path)
        elif self._kind == MACOS_APP:
            _apply_mac(path, mac_bundle_path(self._executable))
        elif self._kind == DEB:
            _relaunch_after_exit(path)
        else:
            raise RuntimeError("This copy of MyEditor can't update itself.")

    @property
    def installs_before_restart(self) -> bool:
        """True when prepare() installs the update for good (the .deb).

        Once that succeeds there is nothing left to undo or to download
        again: the new version is in place and only the restart remains.
        """
        return self._kind == DEB

    def discard_download(self, path: str):
        """Delete a verified download the update was called off for, before
        prepare() ran."""
        _discard(path)

    def discard_prepared(self, path: str):
        """Undo download and prepare() when the swap can't start.

        The downloaded installer or AppImage is deleted and a staged Mac app
        removed. An installed package stays installed: the next launch
        simply is the new version.
        """
        if self._kind == MACOS_APP:
            if path and os.path.basename(path).endswith(".update"):
                shutil.rmtree(path, ignore_errors=True)
        elif self._kind in (WINDOWS_INSTALLER, APPIMAGE):
            _discard(path)

    # -- download plumbing --------------------------------------------------
    def _on_progress(self, received: int, total: int):
        if total > 0:
            self.progress.emit(int(received * 100 / total))

    def _write_chunk(self, chunk: bytes):
        self._fh.write(chunk)
        self._hash.update(chunk)

    def _on_ready_read(self):
        if self._fh is None or self._reply is None:
            return
        try:
            self._write_chunk(bytes(self._reply.readAll()))
        except OSError:
            self._error = _CANNOT_SAVE
            self._reply.abort()

    def _on_finished(self):
        reply = self._reply
        self._reply = None
        net_error = reply.error()
        net_error_text = reply.errorString()
        data = bytes(reply.readAll())
        reply.deleteLater()

        if self._fh is not None:
            try:
                self._write_chunk(data)
            except OSError:
                self._error = self._error or _CANNOT_SAVE
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

        if self._canceled:
            self._discard()
            return
        if self._error:
            self._discard()
            self.failed.emit(self._error, True)
            return
        if net_error != QNetworkReply.NetworkError.NoError:
            self._discard()
            self.failed.emit(download_failure(net_error_text), True)
            return
        if self._expected_size and os.path.getsize(self._dest) != self._expected_size:
            self._discard()
            self.failed.emit("The download was incomplete.", True)
            return
        if self._hash.hexdigest() != self._expected_sha256:
            self._discard()
            self.failed.emit("The download was damaged or changed on the way, "
                             "so it wasn't installed.", True)
            return
        self.ready.emit(self._dest)

    def _discard(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        _discard(self._dest)


_DOWNLOAD_DIR_PREFIX = "my-editor-update-"

_CANNOT_SAVE = "MyEditor couldn't save the download. Check that your disk has free space."


def download_failure(detail: str) -> str:
    """A network error as a sentence. Qt's own text is kept as the reason
    unless it is empty or names the (long, signed) download URL."""
    reason = (detail or "").strip().rstrip(".")
    if not reason or "://" in reason:
        return "The download didn't finish."
    return f"The download didn't finish ({reason})."


def _discard(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
    # The private folder the download was made in goes with it.
    folder = os.path.dirname(path or "")
    if os.path.basename(folder).startswith(_DOWNLOAD_DIR_PREFIX):
        try:
            os.rmdir(folder)
        except OSError:
            pass


# A download folder younger than this may belong to an update another
# MyEditor window is running right now.
_STALE_DOWNLOAD_AGE_S = 24 * 60 * 60


def sweep_stale_downloads(temp_dir: str = None, *, now: float = None) -> None:
    """Remove download folders earlier updates left behind.

    The Windows installer runs from its folder after MyEditor has quit, so
    nothing can delete it then; a later launch does. Only folders this
    updater makes are touched (its prefix, a real folder, owned by this
    account), and only once they are a day old.
    """
    root = temp_dir or tempfile.gettempdir()
    now = time.time() if now is None else now
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(_DOWNLOAD_DIR_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            continue   # never follow a link out of the temp folder
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            continue
        if now - info.st_mtime < _STALE_DOWNLOAD_AGE_S:
            continue
        shutil.rmtree(path, ignore_errors=True)


def _download_destination(kind: str, asset) -> str:
    if kind == APPIMAGE:
        # Same directory as the running AppImage so the later rename is atomic.
        return os.environ["APPIMAGE"] + ".new"
    # A fresh folder only this account can open: nobody else on the machine
    # can swap the checked file for another before it is installed (the .deb
    # is installed as root), and a predictable name cannot be pre-planted.
    folder = tempfile.mkdtemp(prefix=_DOWNLOAD_DIR_PREFIX)
    return os.path.join(folder, os.path.basename(asset.name))


# -- scripts ------------------------------------------------------------------
#
# Each script is built as a string by a pure function, so tests can run
# exactly what will run, against stand-in folders and tools.

def mac_stage_script(dmg_path: str, staged: str) -> str:
    """Open the disk image, copy the app out, close it, check the copy.

    Exit codes map to _MAC_STAGE_ERRORS. The image is mounted privately
    (no Finder window, no desktop icon) and read-only.
    """
    q = shlex.quote
    # Every way out closes the image again, and every failure after the
    # copy started removes the copy: a half-copied app must never be left
    # beside the real one, let alone be taken for the new version.
    detach = ('hdiutil detach "$mnt" -quiet || hdiutil detach "$mnt" -force -quiet; '
              'rmdir "$mnt" 2>/dev/null')
    return (
        f'mnt=$(mktemp -d) || exit 11; '
        f'hdiutil attach -nobrowse -noautoopen -readonly -mountpoint "$mnt" {q(dmg_path)} '
        f'>/dev/null 2>&1 || {{ rmdir "$mnt"; exit 11; }}; '
        f'app=$(find "$mnt" -maxdepth 1 -name "*.app" -type d | head -n 1); '
        f'if [ -z "$app" ]; then {detach}; exit 12; fi; '
        f'rm -rf {q(staged)}; '
        f'ditto "$app" {q(staged)} || {{ rm -rf {q(staged)}; {detach}; exit 13; }}; '
        f'{detach}; '
        f'codesign --verify --deep --strict {q(staged)} >/dev/null 2>&1 '
        f'|| {{ rm -rf {q(staged)}; exit 14; }}; '
        f'exit 0'
    )


def _wait_for_exit(pid: int) -> str:
    return f'p={pid}; while kill -0 "$p" 2>/dev/null; do sleep 0.2; done; '


def mac_swap_script(pid: int, staged: str, bundle: str) -> str:
    """Wait for MyEditor to quit, swap the staged app in, open it.

    If either move fails, the old app is put back and opened, so a failed
    swap leaves a working MyEditor (which then says the update didn't
    install, see MainWindow._report_unfinished_update).
    """
    q = shlex.quote
    old = os.path.join(os.path.dirname(bundle), f".{os.path.basename(bundle)}.previous")
    return (
        _wait_for_exit(pid)
        + f'rm -rf {q(old)}; '
        f'if mv -f {q(bundle)} {q(old)}; then '
        f'if mv -f {q(staged)} {q(bundle)}; then rm -rf {q(old)}; '
        f'else mv -f {q(old)} {q(bundle)}; fi; fi; '
        f'rm -rf {q(staged)}; '
        f'open {q(bundle)}'
    )


def appimage_swap_script(pid: int, new_path: str, appimage: str) -> str:
    """Wait for MyEditor to quit, move the new AppImage over the old one,
    and open whichever is there.

    If the move fails, the new file is removed and the old AppImage opens
    again, so a failed swap still leaves a working MyEditor (which then
    says the update didn't install).
    """
    q = shlex.quote
    return (
        _wait_for_exit(pid)
        + f'if mv -f {q(new_path)} {q(appimage)}; then chmod +x {q(appimage)}; '
        f'else rm -f {q(new_path)}; fi; '
        f'exec {q(appimage)}'
    )


def relaunch_script(pid: int, executable: str) -> str:
    return _wait_for_exit(pid) + f'exec {shlex.quote(executable)}'


def _start_helper(script: str):
    # Runs detached from us, so it outlives this process.
    started, _ = QProcess.startDetached("sh", ["-c", script])
    if not started:
        raise RuntimeError("MyEditor couldn't start the update helper.")


def _apply_windows(installer_path: str):
    # /SILENT shows a small progress window; /CLOSEAPPLICATIONS lets the
    # installer replace the running exe; the installer's [Run] entry relaunches
    # MyEditor once install finishes. startDetached returns (ok, pid) in PySide6.
    started, _ = QProcess.startDetached(
        installer_path,
        ["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS"],
    )
    if not started:
        raise RuntimeError("MyEditor couldn't open the installer.")


def _apply_appimage(new_path: str):
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        raise RuntimeError("MyEditor couldn't find its AppImage.")
    try:
        os.chmod(new_path, 0o755)
    except OSError:
        pass
    _start_helper(appimage_swap_script(os.getpid(), new_path, appimage))


def _apply_mac(staged: str, bundle: str):
    if not bundle or not os.path.isdir(staged):
        raise RuntimeError("The prepared update is missing.")
    _start_helper(mac_swap_script(os.getpid(), staged, bundle))


def _relaunch_after_exit(executable: str):
    _start_helper(relaunch_script(os.getpid(), executable))
