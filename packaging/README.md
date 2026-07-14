# Packaging

One-click installers for MyEditor, built with PyInstaller plus a
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

The whole flow is one command. From a Claude Code session follow
`ReleaseNotesCreator_MyEditor.txt` (it writes the release notes), then run:

```bash
bash packaging/release.sh <version>     # for example: packaging/release.sh 3.0
```

That script bumps `APP_VERSION` in `constants.py`, commits it, pushes the code to
`main`, and pushes the tag `v<version>`. Pushing the tag triggers
`.github/workflows/build-installers.yml`, which builds Windows, macOS (arm64 +
Intel) and Linux and publishes a GitHub Release that uses
`RELEASE_NOTES_v<version>.md` as the body and attaches the three installers as
assets. Nothing is built locally (each installer can only be built on its own OS).

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
- macOS double-click file association via Finder is handled by the app itself:
  `EditorApplication` in `main.py` catches `QFileOpenEvent`, so no
  `argv_emulation` is needed or wanted.
