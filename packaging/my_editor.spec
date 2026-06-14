# -*- mode: python ; coding: utf-8 -*-
"""Shared PyInstaller spec for all three platforms.

Build (from the repo root):

    pyinstaller packaging/my_editor.spec

Outputs:
    Windows / Linux : dist/my-editor/            (onedir folder)
    macOS           : dist/minimal texteditor.app (app bundle) + the folder

The OS wrapper scripts (Inno Setup / create-dmg / appimagetool) turn these into
the final installer for each platform. See packaging/<os>/ and the CI workflow.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

# SPECPATH is the directory containing this spec (packaging/); the repo root is
# its parent. Resolve everything relative to the root so builds are CWD-safe.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
sys.path.insert(0, ROOT)
import constants  # noqa: E402  (needs ROOT on sys.path first)

BINARY = constants.APP_BINARY_NAME        # "my-editor"
DISPLAY = constants.APP_DISPLAY_NAME      # "minimal texteditor"
VERSION = constants.APP_VERSION

ICON_DIR = os.path.join(ROOT, "packaging", "icons")
if sys.platform == "win32":
    icon_file = os.path.join(ICON_DIR, "icon.ico")
elif sys.platform == "darwin":
    icon_file = os.path.join(ICON_DIR, "icon.icns")
else:
    icon_file = None  # Linux: the launcher icon comes from the .desktop entry

# Bundle the window icon, preserving its repo-relative path so resource_path()
# in main.py finds it identically from source and when frozen.
datas = [(os.path.join(ICON_DIR, "icon-256.png"), os.path.join("packaging", "icons"))]

# Static analysis misses these (native ext / lazily-imported Qt submodules).
hiddenimports = [
    "cryptography.hazmat.primitives.ciphers",
    "PySide6.QtNetwork",
    "PySide6.QtWebSockets",
    "PySide6.QtPrintSupport",
]

# coincurve ships compiled CFFI extensions (_libsecp256k1 and a vendored
# _cffi_backend) that the .so files import dynamically at load time, which
# PyInstaller's static analysis cannot see and coincurve ships no hook for.
# Collect every submodule + its dynamic libs so they are importable when frozen.
hiddenimports += collect_submodules("coincurve")
coincurve_binaries = collect_dynamic_libs("coincurve")

# Trim the bundle and make sure the giant, unused Qt WebEngine never sneaks in.
excludes = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtPdf",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "tkinter",
    "unittest",
    "pydoc",
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=coincurve_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=BINARY,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # GUI app: no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,     # macOS file-open via Finder: see note below
    target_arch=None,         # native arch of the build host (CI handles arches)
    codesign_identity=None,   # unsigned for now (see DOWNLOAD.md)
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=BINARY,              # -> dist/my-editor/
)

# macOS: wrap the collected folder into a proper .app bundle.
# Set argv_emulation=True on the EXE above if you later want double-clicking a
# .md/.txt in Finder to open it in a running app (delivers the path via argv).
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{DISPLAY}.app",            # dist/minimal texteditor.app
        icon=icon_file,
        bundle_identifier=constants.APP_BUNDLE_ID,
        version=VERSION,
        info_plist={
            "CFBundleName": DISPLAY,
            "CFBundleDisplayName": DISPLAY,
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.productivity",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Text Document",
                    "CFBundleTypeRole": "Editor",
                    "LSItemContentTypes": [
                        "public.plain-text",
                        "public.html",
                        "net.daringfireball.markdown",
                        "public.json",
                        "public.xml",
                        "public.source-code",
                        "public.python-script",
                    ],
                }
            ],
        },
    )
