# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which app this computer opens a file with, by its real name, and
opening it there.

A file MyEditor does not edit itself (a Word document, a presentation,
a zip) is shown with "Open in Numbers" or "Open in LibreOffice Calc":
the app the person set for that kind of file, asked of the system the
way the Finder, Explorer and the Linux file managers ask. "Open With"
lists the other apps that can open it, and "Show in Finder" (Explorer,
File Manager) shows the file where it is.

  macOS     NSWorkspace through rubicon-objc, as the spell checker
            reaches AppKit (spelling/macos.py)
  Windows   the shell's file associations (AssocQueryStringW,
            SHOpenWithDialog) through ctypes
  Linux     xdg-mime and the .desktop files of the freedesktop.org
            specifications; the desktop portal and the file manager
            over D-Bus

Every platform's code is imported only on that platform, when it is
first used, so this module imports anywhere. Nothing here raises: what
the system cannot answer is None, False or an empty list, and the
reason goes to the log.
"""

from __future__ import annotations

import configparser
import functools
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from i18n import _

log = logging.getLogger(__name__)

MAC = "mac"
WINDOWS = "windows"
LINUX = "linux"


@dataclass(frozen=True)
class AppChoice:
    """An app that can open a file: its name as people know it, and what
    starts it (an app bundle's path on a Mac, a program's path on
    Windows, a desktop file id on Linux)."""
    name: str
    ident: str


def _platform() -> str:
    if sys.platform == "darwin":
        return MAC
    if sys.platform == "win32":
        return WINDOWS
    return LINUX


def _never_raises(fallback):
    """Answer ``fallback`` and log, instead of raising, whatever happens."""
    def wrap(function):
        @functools.wraps(function)
        def safe(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except Exception:                                   # noqa: BLE001
                log.warning("%s failed", function.__name__, exc_info=True)
                return fallback() if callable(fallback) else fallback
        return safe
    return wrap


# The default app's name per extension: asking the system can take a
# moment, and the answer is the same for every file of a kind.
_names: Dict[Tuple[str, str], Optional[str]] = {}


def clear_cache() -> None:
    """Forget the apps found, as after the person chose another default."""
    _names.clear()


def _cache_key(path: str) -> Optional[Tuple[str, str]]:
    ext = os.path.splitext(path)[1].lower()
    return (_platform(), ext) if ext else None


def known_default_app(path: str) -> Tuple[bool, Optional[str]]:
    """``(True, name)`` when the default app for ``path``'s kind of file
    was found before (no need to ask the system again, which can take a
    moment), else ``(False, None)``."""
    key = _cache_key(path)
    if key is not None and key in _names:
        return True, _names[key]
    return False, None


@_never_raises(None)
def default_app_name(path: str) -> Optional[str]:
    """The name of the app this computer opens ``path`` with ("Numbers",
    "Excel", "LibreOffice Calc"), or None when there is none or the
    system cannot say."""
    key = _cache_key(path)
    if key is not None and key in _names:
        return _names[key]
    platform = _platform()
    if platform == MAC:
        name = _mac_default_name(path)
    elif platform == WINDOWS:
        name = _windows_default_name(path)
    else:
        name = _linux_default_name(path)
    name = name.strip() if name else None
    if key is not None:
        _names[key] = name or None
    return name or None


@_never_raises(list)
def other_apps(path: str) -> List[AppChoice]:
    """The apps that can open ``path``, the default one first; may be empty."""
    platform = _platform()
    if platform == MAC:
        choices = _mac_apps(path)
    elif platform == WINDOWS:
        choices = _windows_apps(path)
    else:
        choices = _linux_apps(path)
    unique: List[AppChoice] = []
    for choice in choices:
        if choice.name and all(c.ident != choice.ident for c in unique):
            unique.append(choice)
    return unique


@_never_raises(False)
def open_with_default(path: str) -> bool:
    """Open ``path`` in the app the system opens it with."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(path)))


@_never_raises(False)
def open_with(path: str, choice: AppChoice) -> bool:
    """Open ``path`` in the app ``choice`` (from ``other_apps``)."""
    if not choice.ident:
        return open_with_default(path)
    platform = _platform()
    if platform == MAC:
        return _start(["open", "-a", choice.ident, path], wait=True)
    if platform == WINDOWS:
        return _start([choice.ident, path])
    return _linux_launch(choice.ident, path)


@_never_raises(False)
def choose_app_and_open(path: str, parent=None) -> bool:
    """Let the person pick any app to open ``path`` with, the system's own
    way where there is one; False when nothing was opened."""
    platform = _platform()
    if platform == MAC:
        return _mac_choose(path, parent)
    if platform == WINDOWS:
        return _windows_open_with_dialog(path, parent)
    return _linux_portal_open(path)


@_never_raises(False)
def reveal(path: str) -> bool:
    """Show ``path`` selected in the Finder, Explorer or the file manager."""
    platform = _platform()
    if platform == MAC:
        return _start(["open", "-R", path], wait=True)
    if platform == WINDOWS:
        # Explorer reads "/select," and the quoted path as one argument,
        # and answers 1 even when it did as asked.
        _start(f'explorer /select,"{os.path.normpath(path)}"')
        return True
    if _linux_show_items(path):
        return True
    return open_with_default(os.path.dirname(os.path.abspath(path)))


def reveal_label() -> str:
    """The words for ``reveal`` on this system."""
    platform = _platform()
    if platform == MAC:
        return _("Show in Finder")
    if platform == WINDOWS:
        return _("Show in Explorer")
    return _("Show in File Manager")


def _start(command, *, wait: bool = False) -> bool:
    """Start a program of the system with the person's own environment;
    with ``wait``, whether it said it succeeded."""
    import rmd_toolchain
    kwargs = {"env": rmd_toolchain.program_environment(),
              "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if _platform() == WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    if wait:
        return subprocess.run(command, timeout=15, check=False, **kwargs).returncode == 0
    subprocess.Popen(command, **kwargs)
    return True


def _output(command: List[str]) -> str:
    """What a quick program of the system prints, "" when it fails."""
    import rmd_toolchain
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=5,
                              env=rmd_toolchain.program_environment(), check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as error:
        log.info("%s: %s", command[0], error)
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


# -- macOS ------------------------------------------------------------------


def _mac_appkit():
    """(NSWorkspace, NSURL, NSFileManager) through rubicon-objc."""
    from rubicon.objc import ObjCClass
    from rubicon.objc.runtime import load_library
    load_library("AppKit")
    return ObjCClass("NSWorkspace"), ObjCClass("NSURL"), ObjCClass("NSFileManager")


def _mac_app_name(app_url, file_manager) -> str:
    """An app's name as the Finder shows it, without ".app"."""
    name = str(file_manager.defaultManager.displayNameAtPath(app_url.path))
    return name[:-4] if name.lower().endswith(".app") else name


def _mac_default_name(path: str) -> Optional[str]:
    workspace, url_class, file_manager = _mac_appkit()
    url = url_class.fileURLWithPath(os.path.abspath(path))
    app = workspace.sharedWorkspace.URLForApplicationToOpenURL(url)
    return _mac_app_name(app, file_manager) if app is not None else None


def _mac_apps(path: str) -> List[AppChoice]:
    workspace, url_class, file_manager = _mac_appkit()
    url = url_class.fileURLWithPath(os.path.abspath(path))
    shared = workspace.sharedWorkspace
    choices = []
    default = shared.URLForApplicationToOpenURL(url)
    if default is not None:
        choices.append(AppChoice(_mac_app_name(default, file_manager), str(default.path)))
    apps = shared.URLsForApplicationsToOpenURL(url)
    if apps is not None:
        for app in apps:
            choices.append(AppChoice(_mac_app_name(app, file_manager), str(app.path)))
    return choices


def _mac_choose(path: str, parent) -> bool:
    from PySide6.QtWidgets import QFileDialog
    app, _filter = QFileDialog.getOpenFileName(
        parent, _("Choose an App"), "/Applications", _("Applications (*.app)"))
    if not app:
        return False
    name = os.path.splitext(os.path.basename(app))[0]
    return open_with(path, AppChoice(name, app))


# -- Windows ----------------------------------------------------------------

_ASSOCSTR_EXECUTABLE = 2
_ASSOCSTR_FRIENDLYAPPNAME = 4
_ASSOCF_INIT_IGNOREUNKNOWN = 0x400
_OAIF_ALLOW_REGISTRATION = 0x1
_OAIF_EXEC = 0x4


def _windows_assoc(ext: str, what: int) -> Optional[str]:
    """One of a file type's association strings, from the shell."""
    import ctypes
    from ctypes import wintypes
    query = ctypes.windll.shlwapi.AssocQueryStringW
    query.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = ctypes.c_long
    size = wintypes.DWORD(0)
    flags = _ASSOCF_INIT_IGNOREUNKNOWN
    if query(flags, what, ext, None, None, ctypes.byref(size)) not in (0, 1) or not size.value:
        return None
    buffer = ctypes.create_unicode_buffer(size.value)
    if query(flags, what, ext, None, buffer, ctypes.byref(size)) != 0:
        return None
    return buffer.value or None


def _windows_default_name(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    return _windows_assoc(ext, _ASSOCSTR_FRIENDLYAPPNAME) if ext else None


def _windows_apps(path: str) -> List[AppChoice]:
    """Only the default: Windows keeps the others to its own Open With."""
    ext = os.path.splitext(path)[1].lower()
    name = default_app_name(path)
    if not ext or not name:
        return []
    return [AppChoice(name, _windows_assoc(ext, _ASSOCSTR_EXECUTABLE) or "")]


def _windows_open_with_dialog(path: str, parent) -> bool:
    import ctypes
    from ctypes import wintypes

    class OPENASINFO(ctypes.Structure):
        _fields_ = [("pcszFile", wintypes.LPCWSTR), ("pcszClass", wintypes.LPCWSTR),
                    ("oaifInFlags", ctypes.c_int)]

    info = OPENASINFO(os.path.abspath(path), None, _OAIF_EXEC | _OAIF_ALLOW_REGISTRATION)
    window = int(parent.window().winId()) if parent is not None else None
    dialog = ctypes.windll.shell32.SHOpenWithDialog
    dialog.argtypes = [wintypes.HWND, ctypes.POINTER(OPENASINFO)]
    dialog.restype = ctypes.c_long
    return dialog(window, ctypes.byref(info)) == 0


# -- Linux ------------------------------------------------------------------


def _xdg_dirs(home_variable: str, home_default: str, dirs_variable: str,
              dirs_default: str) -> List[str]:
    home = os.environ.get(home_variable) or os.path.expanduser(home_default)
    others = os.environ.get(dirs_variable) or dirs_default
    return [home] + [d for d in others.split(":") if d]


def _data_dirs() -> List[str]:
    return _xdg_dirs("XDG_DATA_HOME", "~/.local/share", "XDG_DATA_DIRS",
                     "/usr/local/share:/usr/share")


def _config_dirs() -> List[str]:
    return _xdg_dirs("XDG_CONFIG_HOME", "~/.config", "XDG_CONFIG_DIRS", "/etc/xdg")


def _linux_mime_type(path: str) -> Optional[str]:
    mime = _output(["xdg-mime", "query", "filetype", path])
    if mime:
        return mime.split(";", 1)[0].strip()
    from PySide6.QtCore import QMimeDatabase
    found = QMimeDatabase().mimeTypeForFile(path)
    return found.name() if found.isValid() else None


def _desktop_file(desktop_id: str) -> Optional[str]:
    """Where the desktop file with id ``desktop_id`` is: "kde-foo.desktop"
    may be applications/kde/foo.desktop."""
    for data in _data_dirs():
        folder = os.path.join(data, "applications")
        candidate = os.path.join(folder, desktop_id)
        if os.path.isfile(candidate):
            return candidate
        parts = desktop_id.split("-")
        for cut in range(1, len(parts)):
            candidate = os.path.join(folder, *parts[:cut], "-".join(parts[cut:]))
            if os.path.isfile(candidate):
                return candidate
    return None


def _desktop_entry(desktop_id: str) -> Optional[Dict[str, str]]:
    """The [Desktop Entry] keys of a desktop file, None when there is none
    or it is hidden."""
    path = _desktop_file(desktop_id)
    if path is None:
        return None
    parser = configparser.RawConfigParser(strict=False, interpolation=None,
                                          delimiters=("=",), comment_prefixes=("#",))
    parser.optionxform = str
    with open(path, encoding="utf-8", errors="replace") as f:
        parser.read_file(f)
    if not parser.has_section("Desktop Entry"):
        return None
    entry = dict(parser.items("Desktop Entry"))
    if entry.get("Hidden", "").lower() == "true":
        return None
    return entry


def _localized_name(entry: Dict[str, str]) -> Optional[str]:
    """``Name[lang_COUNTRY]``, ``Name[lang]`` or ``Name``, in the app's language."""
    import i18n
    language = i18n.language()
    country = ""
    system = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG")
    if system and system.split("_", 1)[0] == language and "_" in system:
        country = system.split("_", 1)[1].split(".", 1)[0].split("@", 1)[0]
    keys = ([f"Name[{language}_{country}]"] if country else []) + [f"Name[{language}]", "Name"]
    return next((entry[k].strip() for k in keys if entry.get(k, "").strip()), None)


def _linux_default_id(mime: str) -> Optional[str]:
    desktop_id = _output(["xdg-mime", "query", "default", mime])
    if desktop_id:
        return desktop_id.split(";", 1)[0].strip()
    defaults = _mimeapps(mime)[0]
    return defaults[0] if defaults else None


def _linux_default_name(path: str) -> Optional[str]:
    mime = _linux_mime_type(path)
    if not mime:
        return None
    desktop_id = _linux_default_id(mime)
    entry = _desktop_entry(desktop_id) if desktop_id else None
    return _localized_name(entry) if entry else None


def _mimeapps(mime: str) -> Tuple[List[str], List[str], List[str]]:
    """(defaults, added, removed) desktop ids for ``mime`` from every
    mimeapps.list, nearest first."""
    defaults: List[str] = []
    added: List[str] = []
    removed: List[str] = []
    folders = _config_dirs() + [os.path.join(d, "applications") for d in _data_dirs()]
    for folder in folders:
        path = os.path.join(folder, "mimeapps.list")
        groups = read_key_file(path)
        for group, into in (("Default Applications", defaults), ("Added Associations", added),
                            ("Removed Associations", removed)):
            into.extend(i for i in _ids(groups.get(group, {}).get(mime)) if i not in into)
    return defaults, added, removed


def _mime_cache(mime: str) -> List[str]:
    ids: List[str] = []
    for data in _data_dirs():
        groups = read_key_file(os.path.join(data, "applications", "mimeinfo.cache"))
        ids.extend(i for i in _ids(groups.get("MIME Cache", {}).get(mime)) if i not in ids)
    return ids


def _ids(value: Optional[str]) -> List[str]:
    return [part.strip() for part in (value or "").split(";") if part.strip()]


def read_key_file(path: str) -> Dict[str, Dict[str, str]]:
    """The groups of a freedesktop key file, {} when it is not there or
    cannot be read. GTK's settings.ini files are read with it too
    (nostr/ui/native_appearance.py)."""
    if not os.path.isfile(path):
        return {}
    parser = configparser.RawConfigParser(strict=False, interpolation=None,
                                          delimiters=("=",), comment_prefixes=("#",))
    parser.optionxform = str
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as error:
        log.info("Cannot read %s: %s", path, error)
        return {}
    return {section: dict(parser.items(section)) for section in parser.sections()}


def _linux_apps(path: str) -> List[AppChoice]:
    mime = _linux_mime_type(path)
    if not mime:
        return []
    defaults, added, removed = _mimeapps(mime)
    first = _linux_default_id(mime)
    ordered = ([first] if first else []) + defaults + added + _mime_cache(mime)
    choices = []
    for desktop_id in dict.fromkeys(ordered):
        if desktop_id in removed:
            continue
        entry = _desktop_entry(desktop_id)
        if entry is None or entry.get("NoDisplay", "").lower() == "true" or "Exec" not in entry:
            continue
        name = _localized_name(entry)
        if name:
            choices.append(AppChoice(name, desktop_id))
    return choices


def _exec_command(exec_line: str, path: str) -> List[str]:
    """A desktop file's Exec line with the file filled in (the field codes
    of the Desktop Entry specification)."""
    from PySide6.QtCore import QUrl
    command: List[str] = []
    placed = False
    for part in shlex.split(exec_line):
        if part in ("%f", "%F"):
            command.append(path)
            placed = True
        elif part in ("%u", "%U"):
            command.append(bytes(QUrl.fromLocalFile(path).toEncoded()).decode())
            placed = True
        elif part in ("%i", "%c", "%k"):
            continue
        else:
            command.append(part.replace("%%", "%"))
    if not placed:
        command.append(path)
    return command


def _linux_launch(desktop_id: str, path: str) -> bool:
    entry = _desktop_entry(desktop_id)
    if entry is None or not entry.get("Exec"):
        return False
    return _start(_exec_command(entry["Exec"], os.path.abspath(path)))


def _linux_portal_open(path: str) -> bool:
    """The desktop portal's own "Open With" chooser (OpenFile with ask)."""
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusUnixFileDescriptor
    except ImportError:
        return False
    portal = QDBusInterface("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
                            "org.freedesktop.portal.OpenURI", QDBusConnection.sessionBus())
    if not portal.isValid():
        return False
    fd = os.open(path, os.O_RDONLY)
    try:
        reply = portal.call("OpenFile", "", QDBusUnixFileDescriptor(fd), {"ask": True})
    finally:
        os.close(fd)
    return reply.type() != reply.MessageType.ErrorMessage


def _linux_show_items(path: str) -> bool:
    """Ask the file manager (org.freedesktop.FileManager1) to show the file."""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except ImportError:
        return False
    manager = QDBusInterface("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                             "org.freedesktop.FileManager1", QDBusConnection.sessionBus())
    if not manager.isValid():
        return False
    uri = bytes(QUrl.fromLocalFile(os.path.abspath(path)).toEncoded()).decode()
    reply = manager.call("ShowItems", [uri], "")
    return reply.type() != reply.MessageType.ErrorMessage
