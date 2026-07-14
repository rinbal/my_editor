#!/usr/bin/env bash
#
# Assemble the PyInstaller onedir into a portable .AppImage.
#
# Run AFTER `pyinstaller packaging/my_editor.spec`, from anywhere:
#     packaging/linux/build_appimage.sh
#
# Produces: dist/my-editor-<version>-linux-<arch>.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

DIST_APP="dist/my-editor"
if [ ! -d "$DIST_APP" ]; then
  echo "error: $DIST_APP not found. Run pyinstaller packaging/my_editor.spec first." >&2
  exit 1
fi

# Single source of truth for the version (parsed without importing PySide6).
VERSION="$(grep -E '^APP_VERSION' constants.py | sed -E 's/.*"([^"]+)".*/\1/')"
ARCH="$(uname -m)"   # x86_64 on the CI runner

APPDIR="dist/my-editor.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib/my-editor"

# The PyInstaller onedir (executable + _internal/) goes under usr/lib.
cp -r "$DIST_APP/." "$APPDIR/usr/lib/my-editor/"

# AppImage requires the icon and .desktop at the AppDir root; the icon basename
# must match Icon= in the .desktop entry (my-editor -> my-editor.png).
cp packaging/icons/icon-256.png "$APPDIR/my-editor.png"
cp packaging/linux/my-editor.desktop "$APPDIR/my-editor.desktop"
cp packaging/linux/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# AppStream metainfo lets software centers and AppImage tooling show a
# description, summary and icon for the app.
mkdir -p "$APPDIR/usr/share/metainfo"
cp packaging/linux/my-editor.appdata.xml "$APPDIR/usr/share/metainfo/my-editor.appdata.xml"

# Get appimagetool (prefer one on PATH; otherwise download the static build).
TOOL="$(command -v appimagetool || true)"
if [ -z "$TOOL" ]; then
  TOOL="dist/appimagetool-x86_64.AppImage"
  if [ ! -x "$TOOL" ]; then
    echo "Downloading appimagetool..."
    curl -fsSL -o "$TOOL" \
      "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    chmod +x "$TOOL"
  fi
fi

OUT="dist/my-editor-${VERSION}-linux-${ARCH}.AppImage"
rm -f "$OUT"

# --appimage-extract-and-run lets appimagetool run without FUSE on CI runners.
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"

echo "Built $OUT"
