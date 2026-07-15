# Install MyEditor

Pick your system, download one file, and open it. MyEditor is not yet signed
with a paid certificate, so your computer shows a one-time safety prompt the
first time. Each guide below walks you through it. After that, MyEditor opens
with a normal double-click.

All downloads are on the
[Releases page](https://github.com/rinbal/my_editor/releases/latest).

---

## Windows

1. Download **`my-editor-x.y.z-windows-setup.exe`**.
2. Double-click it. If a blue **"Windows protected your PC"** box appears, click
   **More info**, then **Run anyway**.
3. Click through the installer. It installs for you only and never asks for an
   administrator password.

MyEditor launches when it finishes. You will also find it in the Start Menu and
on your desktop.

> Remove it later from **Settings > Apps > MyEditor > Uninstall**.

---

## macOS

1. Download **`my-editor-x.y.z-macos-arm64.dmg`**. On an older Intel Mac, use
   the **`-intel`** file.
2. Open the file. In the window that appears, drag the **MyEditor** icon onto
   the **Applications** folder, following the arrow.
3. Open your **Applications** folder, **right-click** MyEditor, and choose
   **Open**.
4. macOS asks once if you are sure. Click **Open**.

Done. MyEditor now opens with a normal double-click.

> Step 3 matters: **right-click, then Open**. A plain double-click only shows a
> **Done** button and will not open the app the first time.

> Says "damaged and can't be opened"? The download was quarantined. Open the
> **Terminal** app, paste this line, press Return, then open MyEditor again:
> `xattr -dr com.apple.quarantine "/Applications/MyEditor.app"`

> Apple Silicon or Intel? Apple menu > **About This Mac**. "Apple M1/M2/M3..."
> uses the **arm64** file; "Intel" uses the **-intel** file.

---

## Linux

**Ubuntu, Debian, Mint, or Pop!_OS (easiest):**

1. Download **`my-editor_x.y.z_amd64.deb`**.
2. Double-click it, then click **Install** (it may ask for your password).
3. Open MyEditor from your applications menu.

Everything it needs is installed for you. Remove it with
`sudo apt remove my-editor`.

**Any other distribution:**

1. Download **`my-editor-x.y.z-linux-x86_64.AppImage`**.
2. Make it executable: right-click > **Properties** > **Permissions** > tick
   **Allow executing file as program** (or run `chmod +x my-editor-*.AppImage`).
3. Double-click it to run.

This one file is the whole app. Delete it to remove the program. To add a menu
entry, use [AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher).

> AppImage will not start? Install one common library:
> `sudo apt install libxcb-cursor0`. The `.deb` above pulls it in for you.

---

## Where your files and settings live

Your notes stay wherever you save them. MyEditor keeps its own settings and
cache in `~/.config/my_editor` and `~/.cache/my_editor` on every system.
Removing the app leaves these in place. Delete those two folders for a full
cleanup.
