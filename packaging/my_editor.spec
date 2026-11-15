# -*- mode: python ; coding: utf-8 -*-
"""Shared PyInstaller spec for all three platforms.

Build (from the repo root):

    pyinstaller packaging/my_editor.spec

Outputs:
    Windows / Linux : dist/my-editor/            (onedir folder)
    macOS           : dist/MyEditor.app (app bundle) + the folder

The OS wrapper scripts (Inno Setup / create-dmg / appimagetool) turn these into
the final installer for each platform. See packaging/<os>/ and the CI workflow.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, copy_metadata

# SPECPATH is the directory containing this spec (packaging/); the repo root is
# its parent. Resolve everything relative to the root so builds are CWD-safe.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
sys.path.insert(0, ROOT)
import constants  # noqa: E402  (needs ROOT on sys.path first)

BINARY = constants.APP_BINARY_NAME        # "my-editor"
DISPLAY = constants.APP_DISPLAY_NAME      # "MyEditor"
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
# The translations (i18n.py reads locale/<language>.po at startup).
datas += [(os.path.join(ROOT, "locale", name), "locale")
          for name in sorted(os.listdir(os.path.join(ROOT, "locale")))
          if name.endswith(".po")]
# The stylesheet and scripts an exported page with photo groups carries
# (export_html.py inlines web/ in the page).
datas += [(os.path.join(ROOT, "web", name), "web")
          for name in sorted(os.listdir(os.path.join(ROOT, "web")))
          if name.endswith((".css", ".js"))]

# Static analysis misses these (native ext / lazily-imported Qt submodules).
hiddenimports = [
    "cryptography.hazmat.primitives.ciphers",
    "PySide6.QtNetwork",
    "PySide6.QtWebSockets",
    "PySide6.QtPrintSupport",
    # Local Quick Look playback and video thumbnails. PyInstaller's Qt
    # hooks collect the multimedia backend plugins and their libraries.
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    # Edit > Speech (speech.py), imported at the first word read; the Qt
    # hook brings the system's voice engines (texttospeech plugins).
    "PySide6.QtTextToSpeech",
    # Full-text recovery (nostr/imports/fulltext.py): readability-lxml
    # rides on lxml's compiled extensions, which static analysis can
    # only partially see.
    "readability",
    "lxml.etree",
    "lxml.html",
    "lxml.html.clean",
    # Spreadsheets (sheet_io.py), imported when one is opened: openpyxl
    # for .xlsx and .xlsm, lxml.etree (above) for .ods.
    "openpyxl",
    "et_xmlfile",
]

# coincurve ships compiled CFFI extensions (_libsecp256k1 and a vendored
# _cffi_backend) that the .so files import dynamically at load time, which
# PyInstaller's static analysis cannot see and coincurve ships no hook for.
# Collect every submodule + its dynamic libs so they are importable when frozen.
hiddenimports += collect_submodules("coincurve")
coincurve_binaries = collect_dynamic_libs("coincurve")

# Spell checking (spelling/) uses the system's own checker. Its backend
# module is imported by platform when checking starts, so name it here.
# macOS reaches NSSpellChecker through rubicon-objc, which reads its own
# version from its package metadata when imported.
if sys.platform == "darwin":
    hiddenimports += ["spelling.macos"] + collect_submodules("rubicon.objc")
    datas += copy_metadata("rubicon-objc")
elif sys.platform == "win32":
    hiddenimports += ["spelling.windows"]   # ctypes and COM only, nothing to collect
else:
    # Enchant is the system's library, loaded at run time and deliberately
    # not bundled: it finds its providers and dictionaries where it was
    # installed. The .deb recommends it (build_deb.sh).
    hiddenimports += ["spelling.enchant"]

# Dictation on this computer (dictation/on_device.py), on Windows and Linux:
# sherpa-onnx, its compiled module and ONNX Runtime beside it, imported only
# when someone dictates. A Mac dictates with the system's own.
dictation_binaries = []
if sys.platform != "darwin":
    hiddenimports += collect_submodules("sherpa_onnx")
    dictation_binaries = collect_dynamic_libs("sherpa_onnx")

# QtPdf backs the built-in PDF viewer (pdf_viewer.py) and must ship in
# every bundle. Trim the rest and make sure the giant, unused Qt
# WebEngine never sneaks in.
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
    binaries=coincurve_binaries + dictation_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

# Windows-only: a version resource so Explorer's file Properties dialog shows
# product/version metadata for my-editor.exe. The import lives behind the
# platform check since PyInstaller.utils.win32.versioninfo pulls in Windows-only
# dependencies; version_info stays None (no-op) on Linux/macOS.
version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo,
        FixedFileInfo,
        StringFileInfo,
        StringTable,
        StringStruct,
        VarFileInfo,
        VarStruct,
    )

    _version_parts = tuple(int(p) for p in VERSION.split("."))
    _version_tuple = _version_parts + (0,) * (4 - len(_version_parts))

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_version_tuple,
            prodvers=_version_tuple,
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "rinbal"),
                            StringStruct("FileDescription", DISPLAY),
                            StringStruct("FileVersion", VERSION),
                            StringStruct("ProductVersion", VERSION),
                            StringStruct("ProductName", DISPLAY),
                            StringStruct("OriginalFilename", "my-editor.exe"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )

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
    argv_emulation=False,     # macOS Finder file-opens are handled in-app via QFileOpenEvent, not argv
    target_arch=None,         # native arch of the build host (CI handles arches)
    codesign_identity=None,   # unsigned for now (see docs/install.md)
    entitlements_file=None,
    icon=icon_file,
    version=version_info,
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
# Double-clicking a .md/.txt in Finder is handled by the app itself: main.py
# defines EditorApplication, which overrides event() to catch QFileOpenEvent
# (the odoc Apple event Qt turns file-opens into). argv_emulation stays False
# above because PyInstaller recommends against it for GUI toolkits: it only
# covers the initial launch and can interfere with the toolkit's own event
# handling.
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{DISPLAY}.app",            # dist/MyEditor.app
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
            # The languages the app speaks, so macOS shows its own panels
            # (Open, Save, the app menu) in the same one.
            "CFBundleLocalizations": ["en", "de"],
            "CFBundleDevelopmentRegion": "en",
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
                },
                # Read-only PDF viewing. Rank Alternate so the app shows
                # up under Finder's "Open With" without ever contesting
                # Preview as the system default.
                {
                    "CFBundleTypeName": "PDF Document",
                    "CFBundleTypeRole": "Viewer",
                    "LSHandlerRank": "Alternate",
                    "LSItemContentTypes": ["com.adobe.pdf"],
                },
            ],
        },
    )
