#!/usr/bin/env bash
#
# Assemble the PyInstaller onedir into a double-click .deb package for
# Debian / Ubuntu / Mint / Pop!_OS (the largest desktop-Linux base).
#
# Run AFTER `pyinstaller packaging/my_editor.spec`, from anywhere:
#     packaging/linux/build_deb.sh
#
# Produces: dist/my-editor_<version>_<arch>.deb
#
# The AppImage (build_appimage.sh) stays the portable, any-distro option. This
# .deb is the "install like normal software" path: double-clicking it opens the
# distro's Software app, it lands in the application menu automatically, and apt
# pulls the Qt runtime libraries so there is no manual "install libxcb-cursor0"
# step for the user.
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

# Debian architecture names differ from uname -m.
case "$(uname -m)" in
  x86_64)  ARCH="amd64" ;;
  aarch64) ARCH="arm64" ;;
  armv7l)  ARCH="armhf" ;;
  *)       ARCH="$(uname -m)" ;;
esac

STAGE="dist/my-editor_${VERSION}_${ARCH}"
OUT="dist/my-editor_${VERSION}_${ARCH}.deb"
rm -rf "$STAGE" "$OUT"

# Filesystem layout the package installs:
#   /opt/my-editor/                 the PyInstaller onedir (binary + _internal/)
#   /usr/bin/my-editor              symlink onto PATH (PyInstaller resolves the
#                                   real path via /proc/self/exe, so _internal
#                                   is still found under /opt)
#   /usr/share/applications/...     desktop launcher (menu entry + file types)
#   /usr/share/icons/hicolor/...    app icon for the menu and window
#   /usr/share/metainfo/...         AppStream data for the Software app
mkdir -p "$STAGE/opt/my-editor" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps" \
         "$STAGE/usr/share/metainfo" \
         "$STAGE/DEBIAN"

cp -r "$DIST_APP/." "$STAGE/opt/my-editor/"
ln -s /opt/my-editor/my-editor "$STAGE/usr/bin/my-editor"
cp packaging/linux/my-editor.desktop "$STAGE/usr/share/applications/my-editor.desktop"
cp packaging/icons/icon-256.png "$STAGE/usr/share/icons/hicolor/256x256/apps/my-editor.png"
cp packaging/linux/my-editor.appdata.xml "$STAGE/usr/share/metainfo/my-editor.appdata.xml"

# Installed size in KiB (dpkg convention), excluding the control files.
INSTALLED_SIZE="$(du -sk "$STAGE/opt" "$STAGE/usr" | awk '{total += $1} END {print total}')"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: my-editor
Version: ${VERSION}
Section: editors
Priority: optional
Architecture: ${ARCH}
Maintainer: rinbal <rinbal@users.noreply.github.com>
Installed-Size: ${INSTALLED_SIZE}
Depends: libc6, libxcb-cursor0, libegl1, libxkbcommon0
Homepage: https://github.com/rinbal/my_editor
Description: Minimal note-taking text editor
 A clean, distraction-free desktop text editor. Write locally, publish to
 Nostr, and keep your keys in your own signer. Supports multiple tabs, dark
 and light themes, syntax highlighting, and crash recovery.
EOF

# Refresh the menu and icon caches so the launcher shows up immediately, with
# no logout. Guarded so the package still installs on minimal systems.
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi
exit 0
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
    fi
fi
exit 0
EOF

chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# --root-owner-group makes the packaged files root:root without needing fakeroot
# or root (dpkg >= 1.19, present on Ubuntu 20.04+ / the CI runner).
dpkg-deb --root-owner-group --build "$STAGE" "$OUT"
rm -rf "$STAGE"

echo "Built $OUT"
