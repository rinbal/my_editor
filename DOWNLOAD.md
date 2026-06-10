# Download minimal texteditor

Pick your system, download the file, and open it. The app is currently
**unsigned**, so the first time you open it your operating system shows a
one-time safety prompt. The steps below walk you through it. After the first
launch it opens normally with a double-click.

The downloads live on the project's
[Releases page](https://github.com/DoktorShift/my_editor/releases/latest).

---

## Windows

1. Download **`my-editor-x.y.z-windows-setup.exe`**.
2. Double-click it. Windows may show a blue **"Windows protected your PC"**
   box.
3. Click **More info**, then click the **Run anyway** button that appears.
4. Follow the installer (Next, Next, Finish). It installs for your user only,
   so it does not ask for an administrator password.
5. Launch **minimal texteditor** from the Start Menu.

To uninstall later: Settings > Apps > minimal texteditor > Uninstall.

---

## macOS

1. Download **`my-editor-x.y.z-macos-arm64.dmg`** (use the `-intel` file if you
   have an older Intel Mac).
2. Open the `.dmg` and drag **minimal texteditor** onto the **Applications**
   folder.
3. Open **Applications** and double-click the app. macOS says it
   "cannot be opened because Apple cannot check it for malicious software."
   Click **Done**.
4. Open **System Settings > Privacy & Security**, scroll down, and click
   **Open Anyway** next to the message about minimal texteditor. Confirm with
   **Open**.
5. From now on it opens with a normal double-click.

> Not sure if your Mac is Apple Silicon or Intel? Click the Apple menu >
> About This Mac. "Apple M1/M2/M3..." means Apple Silicon (use the `arm64`
> file); "Intel" means use the `-intel` file.

---

## Linux

1. Download **`my-editor-x.y.z-linux-x86_64.AppImage`**.
2. Make it executable, either by:
   - right-clicking the file > **Properties** > **Permissions** > tick
     **Allow executing file as program**, or
   - running in a terminal: `chmod +x my-editor-*.AppImage`
3. Double-click the AppImage (or run `./my-editor-*.AppImage` in a terminal).

If it does not start, install the small dependencies most distros already have:

```bash
# Debian / Ubuntu
sudo apt install libfuse2 libxcb-cursor0
```

There is no installer to run and nothing to uninstall: the single AppImage file
*is* the app. Delete it to remove the program.

---

## Where my files and settings are stored

Your notes stay wherever you save them. The app keeps its own settings and
caches in:

- **Windows / Linux:** `~/.config/my_editor` and `~/.cache/my_editor`
- **macOS:** `~/.config/my_editor` and `~/.cache/my_editor`

Removing the app does not delete these; delete those folders too for a full
cleanup.
