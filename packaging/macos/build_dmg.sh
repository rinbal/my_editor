#!/usr/bin/env bash
#
# Wrap the PyInstaller .app bundle into a styled drag-to-install .dmg.
#
# Run AFTER `pyinstaller packaging/my_editor.spec`, from anywhere:
#     packaging/macos/build_dmg.sh
#
# Produces: dist/my-editor-<version>-macos-<arch>.dmg
#
# Uses dmgbuild (pure Python) to lay out the Finder window: the drag-to-
# Applications background, the app icon on the left, the Applications folder on
# the right, and the arrow between them. dmgbuild writes the window layout into
# the image's .DS_Store directly, so unlike create-dmg it needs no GUI/Finder
# session and works on headless CI runners.
set -euo pipefail

# Resolve repo root (this script lives in packaging/macos/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

APP_PATH="$(ls -d dist/*.app 2>/dev/null | head -1 || true)"
if [ -z "$APP_PATH" ]; then
  echo "error: no .app found in dist/. Run pyinstaller packaging/my_editor.spec first." >&2
  exit 1
fi

APP_NAME="$(basename "$APP_PATH")"          # e.g. "MyEditor.app"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$APP_PATH/Contents/Info.plist")"
ARCH="$(uname -m)"                          # arm64 or x86_64
OUT="dist/my-editor-${VERSION}-macos-${ARCH}.dmg"

PY="${PYTHON:-python3}"

# dmgbuild is a build-time tool (not in requirements.txt); install on demand so
# this works both on CI and on a maintainer's machine.
if ! "$PY" -c 'import dmgbuild' >/dev/null 2>&1; then
  echo "Installing dmgbuild..."
  "$PY" -m pip install --quiet --disable-pip-version-check dmgbuild
fi

# Prefer the Retina TIFF background; fall back to the standard-resolution PNG.
BG="packaging/macos/dmg_background.tiff"
[ -f "$BG" ] || BG="packaging/macos/dmg_background.png"

echo "Packaging $APP_NAME (v$VERSION, $ARCH) -> $OUT"
rm -f "$OUT"

"$PY" -m dmgbuild \
  -s packaging/macos/dmg_settings.py \
  -D app="$APP_PATH" \
  -D background="$BG" \
  "Install MyEditor" \
  "$OUT"

echo "Built $OUT"
