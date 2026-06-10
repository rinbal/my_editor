# Packaging

One-click installers for minimal texteditor, built with PyInstaller plus a
per-OS wrapper. End users get instructions in the top-level `DOWNLOAD.md`.

```
packaging/
  my_editor.spec        shared PyInstaller spec (all platforms)
  icons/                app icons + make_icons.py generator
  windows/installer.iss Inno Setup installer script
  macos/build_dmg.sh    wraps the .app into a .dmg
  linux/                AppRun, .desktop, build_appimage.sh
```

## How a release happens

1. Bump `APP_VERSION` in `constants.py`.
2. Commit, then tag: `git tag v1.0.0 && git push --tags`.
3. GitHub Actions (`.github/workflows/build-installers.yml`) builds Windows,
   macOS (arm64 + Intel) and Linux, then attaches the installers to a GitHub
   Release. Link those release assets from the homepage.

Use the **Run workflow** button on the Actions tab (workflow_dispatch) to build
without tagging; it uploads the installers as artifacts but does not publish a
release.

## Building locally

Each OS can only build its own installer (no cross-compiling).

Dev tools (in addition to `requirements.txt`):

```bash
pip install pyinstaller pillow      # pillow only needed to regenerate icons
```

Common first step on every OS:

```bash
pyinstaller --noconfirm packaging/my_editor.spec
```

Then:

- **macOS:** `packaging/macos/build_dmg.sh` -> `dist/my-editor-<ver>-macos-<arch>.dmg`
- **Linux:** `packaging/linux/build_appimage.sh` -> `dist/my-editor-<ver>-linux-<arch>.AppImage`
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

## Notes

- The build is **unsigned**. To remove the OS security prompts later, add Apple
  notarization to the macOS job and an Authenticode signing step to the Windows
  job; nothing else in the pipeline changes.
- QtWebEngine is excluded in the spec (the app does not use it), which keeps
  bundles around 100-200 MB instead of 500 MB+.
- macOS double-click file association via Finder needs `argv_emulation=True` in
  the spec (off by default to keep the launch path simple).
