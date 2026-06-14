#!/usr/bin/env bash
#
# Wrap the PyInstaller .app bundle into a drag-to-install .dmg.
#
# Run AFTER `pyinstaller packaging/my_editor.spec`, from anywhere:
#     packaging/macos/build_dmg.sh
#
# Produces: dist/my-editor-<version>-macos-<arch>.dmg
#
# Uses hdiutil only (no create-dmg): create-dmg styles the window via AppleScript
# which needs a GUI/Finder session and fails on headless CI runners. The plain
# DMG still gives the standard "drag the app onto Applications" experience.
set -euo pipefail

# Resolve repo root (this script lives in packaging/macos/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

APP_PATH="$(ls -d dist/*.app 2>/dev/null | head -1 || true)"
if [ -z "$APP_PATH" ]; then
  echo "error: no .app found in dist/. Run pyinstaller packaging/my_editor.spec first." >&2
  exit 1
fi

APP_NAME="$(basename "$APP_PATH")"          # e.g. "minimal texteditor.app"
VOL_NAME="${APP_NAME%.app}"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP_PATH/Contents/Info.plist")"
ARCH="$(uname -m)"                          # arm64 or x86_64
OUT="dist/my-editor-${VERSION}-macos-${ARCH}.dmg"

echo "Packaging $APP_NAME (v$VERSION, $ARCH) -> $OUT"

# Stage the app plus an /Applications symlink so users can drag-to-install.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP_PATH" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$OUT"
hdiutil create \
  -volname "$VOL_NAME" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -ov \
  "$OUT"

echo "Built $OUT"
