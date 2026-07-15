# Packaging

One-click installers for MyEditor, built with PyInstaller plus a
per-OS wrapper. End users get instructions in [`docs/install.md`](../docs/install.md).

```
packaging/
  my_editor.spec        shared PyInstaller spec (all platforms)
  icons/                app icons + make_icons.py generator
  windows/installer.iss Inno Setup installer wizard
  macos/                build_dmg.sh + the styled drag-to-Applications background
  linux/                AppRun, .desktop, build_appimage.sh, build_deb.sh
```

Each platform gives users the pattern they expect: a click-through wizard on
Windows, a styled drag-to-Applications disk image on macOS, and a double-click
`.deb` on Debian/Ubuntu (with the portable AppImage as the any-distro fallback).

## How a release happens

The whole flow is one command. Follow
[`docs/release-process.md`](../docs/release-process.md) (it writes the release notes), then run:

```bash
bash packaging/release.sh <version>     # for example: packaging/release.sh 3.0
```

That script bumps `APP_VERSION` in `constants.py`, commits it, pushes the code to
`main`, and pushes the tag `v<version>`. Pushing the tag triggers
`.github/workflows/build-installers.yml`, which builds Windows, macOS (arm64 +
Intel) and Linux and publishes a GitHub Release that uses
`docs/releases/v<version>.md` as the body and attaches every platform installer
as assets. Nothing is built locally (each installer can only be built on its own OS).

Use the **Run workflow** button on the Actions tab (workflow_dispatch) to build
without tagging; it uploads the installers as artifacts but does not publish a
release.

## Building locally

Each OS can only build its own installer (no cross-compiling).

Dev tools (in addition to `requirements.txt`):

```bash
pip install pyinstaller pillow      # pillow only needed to regenerate icons/background
```

Common first step on every OS:

```bash
pyinstaller --noconfirm packaging/my_editor.spec
```

Then:

- **macOS:** `packaging/macos/build_dmg.sh` -> `dist/my-editor-<ver>-macos-<arch>.dmg`
  (installs `dmgbuild` on demand to lay out the drag-to-Applications window)
- **Linux:** `packaging/linux/build_appimage.sh` -> `dist/my-editor-<ver>-linux-<arch>.AppImage`
  and `packaging/linux/build_deb.sh` -> `dist/my-editor_<ver>_<arch>.deb`
- **Windows:** install [Inno Setup](https://jrsoftware.org/isinfo.php), then
  `iscc /DMyAppVersion=<ver> packaging\windows\installer.iss`
  -> `dist\my-editor-<ver>-windows-setup.exe`

## Regenerating icons

Edit the design in `icons/make_icons.py` (or drop in your own master `icon.png`)
and run:

```bash
python packaging/icons/make_icons.py
```

The committed `icon.ico` / `icon.icns` / `icon-256.png` are what the build uses;
`.icns` is only regenerated on macOS (needs `iconutil`).

## Regenerating the macOS DMG background

The drag-to-Applications background (`macos/dmg_background.png` plus the Retina
`macos/dmg_background.tiff`) is committed and used as-is by the build. To change
it, edit `macos/make_dmg_background.py` and regenerate on a Mac (needs Pillow;
`tiffutil` ships with macOS):

```bash
python packaging/macos/make_dmg_background.py
```

Keep the icon centers in the generator in sync with `icon_locations` in
`macos/dmg_settings.py` so the arrow lines up with the two icons.

## Notes

- The build is **unsigned**. To remove the OS security prompts later, add Apple
  notarization to the macOS job and an Authenticode signing step to the Windows
  job; nothing else in the pipeline changes.
- The macOS DMG window is styled by `dmgbuild`, which writes the Finder layout
  directly into the image's `.DS_Store`. It needs no GUI/Finder session, so it
  works on the headless CI runner (plain `hdiutil` could not style the window).
- The Linux `.deb` installs the app under `/opt/my-editor`, symlinks
  `/usr/bin/my-editor` onto `PATH`, adds the menu launcher and icon, and declares
  the Qt runtime libraries as dependencies so `apt` pulls them in automatically.
- QtWebEngine is excluded in the spec (the app does not use it), which keeps
  bundles around 100-200 MB instead of 500 MB+.
- macOS double-click file association via Finder is handled by the app itself:
  `EditorApplication` in `main.py` catches `QFileOpenEvent`, so no
  `argv_emulation` is needed or wanted.
