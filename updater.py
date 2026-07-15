#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-app updater for the packaged builds that can safely replace themselves.

Only two install formats are updated in place, because only these can be swapped
without code signing or a system package manager:

    Windows (Inno Setup) : download the new -windows-setup.exe and run it
                           silently; it closes the app, updates, and relaunches.
    Linux AppImage       : download the new .AppImage next to the running one,
                           then a tiny helper waits for the app to quit, swaps
                           the file, and relaunches it.

For every other case (macOS .app, the Linux .deb, or a source checkout) there is
nothing safe to swap, so supports_in_app_update() returns False and the caller
falls back to opening the release page in the browser.
"""

import os
import platform
import shlex
import sys
import tempfile

from PySide6.QtCore import QObject, QProcess, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

# Install kinds.
WINDOWS_INSTALLER = "windows_installer"
APPIMAGE = "appimage"
MACOS_APP = "macos_app"
LINUX_OTHER = "linux_other"   # .deb install or a bare onedir
SOURCE = "source"


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
    return LINUX_OTHER


def supports_in_app_update(kind: str = None) -> bool:
    """True only for the formats we can replace in place (see module docstring)."""
    kind = kind or detect_install_kind()
    if kind == WINDOWS_INSTALLER:
        return True
    if kind == APPIMAGE:
        return _appimage_writable()
    return False


def select_asset(kind: str, assets):
    """Pick the release asset that matches this install, or None if there is none."""
    if kind == WINDOWS_INSTALLER:
        return _first(assets, lambda a: a.name.lower().endswith("-windows-setup.exe"))
    if kind == APPIMAGE:
        # Require the CPU arch in the name so we never swap in a wrong-arch build
        # (that would replace the app with one that cannot run). The AppImage is
        # named ...-linux-<arch>.AppImage; a mismatch returns None and the caller
        # falls back to opening the release page.
        arch = platform.machine().lower()
        return _first(
            assets,
            lambda a: a.name.lower().endswith(".appimage") and arch in a.name.lower(),
        )
    return None


def _first(items, predicate):
    for item in items:
        if predicate(item):
            return item
    return None


def _appimage_writable() -> bool:
    path = os.environ.get("APPIMAGE")
    return bool(path) and os.access(os.path.dirname(path) or ".", os.W_OK)


class UpdateInstaller(QObject):
    """Downloads a release asset and applies it for the current install kind."""

    progress = Signal(int)   # 0..100 percent
    ready = Signal(str)      # local path of the downloaded update, once complete
    failed = Signal(str)

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._manager = QNetworkAccessManager(self)
        self._reply = None
        self._fh = None
        self._dest = None
        self._expected_size = 0
        self._canceled = False
        self._error = None

    def start(self, asset):
        try:
            self._dest = _download_destination(self._kind, asset)
            self._fh = open(self._dest, "wb")
        except OSError as exc:
            self.failed.emit(f"Cannot write the update file: {exc}")
            return
        self._expected_size = asset.size or 0

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

    def apply(self, path: str):
        """Launch the swap. The caller must quit the app right after this."""
        if self._kind == WINDOWS_INSTALLER:
            _apply_windows(path)
        elif self._kind == APPIMAGE:
            _apply_appimage(path)
        else:
            raise RuntimeError("in-app update is not supported for this install")

    # -- download plumbing --------------------------------------------------
    def _on_progress(self, received: int, total: int):
        if total > 0:
            self.progress.emit(int(received * 100 / total))

    def _on_ready_read(self):
        if self._fh is None or self._reply is None:
            return
        try:
            self._fh.write(bytes(self._reply.readAll()))
        except OSError as exc:
            self._error = f"Cannot write the update file: {exc}"
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
                self._fh.write(data)
            except OSError as exc:
                self._error = self._error or f"Cannot save the update file: {exc}"
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
            self.failed.emit(self._error)
            return
        if net_error != QNetworkReply.NetworkError.NoError:
            self._discard()
            self.failed.emit(net_error_text)
            return
        if self._expected_size and os.path.getsize(self._dest) != self._expected_size:
            self._discard()
            self.failed.emit("The download was incomplete.")
            return
        self.ready.emit(self._dest)

    def _discard(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self._dest and os.path.exists(self._dest):
            try:
                os.remove(self._dest)
            except OSError:
                pass


def _download_destination(kind: str, asset) -> str:
    if kind == APPIMAGE:
        # Same directory as the running AppImage so the later rename is atomic.
        return os.environ["APPIMAGE"] + ".new"
    return os.path.join(tempfile.gettempdir(), os.path.basename(asset.name))


def _apply_windows(installer_path: str):
    # /SILENT shows a small progress window; /CLOSEAPPLICATIONS lets the
    # installer replace the running exe; the installer's [Run] entry relaunches
    # MyEditor once install finishes. startDetached returns (ok, pid) in PySide6.
    started, _ = QProcess.startDetached(
        installer_path,
        ["/SILENT", "/SUPPRESSMSGBOXES", "/CLOSEAPPLICATIONS", "/NORESTARTAPPLICATIONS"],
    )
    if not started:
        raise RuntimeError("Could not launch the installer.")


def _apply_appimage(new_path: str):
    appimage = os.environ["APPIMAGE"]
    try:
        os.chmod(new_path, 0o755)
    except OSError:
        pass
    # Wait for this process to exit (so the file is no longer mounted), swap the
    # new AppImage over the old one, then relaunch it. Runs detached from us.
    pid = os.getpid()
    script = (
        f'p={pid}; while kill -0 "$p" 2>/dev/null; do sleep 0.2; done; '
        f'mv -f {shlex.quote(new_path)} {shlex.quote(appimage)} && '
        f'chmod +x {shlex.quote(appimage)} && exec {shlex.quote(appimage)}'
    )
    started, _ = QProcess.startDetached("sh", ["-c", script])
    if not started:
        raise RuntimeError("Could not launch the update helper.")
