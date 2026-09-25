# Install MyEditor

Pick your system, download one file, and open it. MyEditor is not yet signed
with a paid certificate, so your computer shows a one-time safety prompt the
first time. Each guide below walks you through it. After that, MyEditor opens
with a normal double-click.

The easiest way is the
**[step-by-step install guide](https://rinbal.github.io/my_editor/install/)**.
It picks the right file for your computer and shows every prompt with a picture
of what you will see. The same steps follow here as text. All downloads are
also on the [Releases page](https://github.com/rinbal/my_editor/releases/latest).

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

1. Download **`my-editor-x.y.z-macos-arm64.dmg`**. On a Mac with an Intel
   processor, use the **`-macos-x86_64`** file.
2. Open the file. In the window that appears, drag the **MyEditor** icon onto
   the **Applications** folder, following the arrow.
3. Open your **Applications** folder and double-click MyEditor. macOS says it
   can't verify the app. Click **Done**, not Move to Trash.
4. Open **System Settings > Privacy & Security** and scroll down to
   **Security**. Click **Open Anyway** next to the message about MyEditor.
5. macOS asks one last time. Click **Open Anyway**, then use Touch ID or enter
   your login password.

Done. MyEditor now opens with a normal double-click. Eject the **Install
MyEditor** disk in the Finder sidebar and move the downloaded file to the Trash.

> The **Open Anyway** button appears for about an hour after step 3. If it is
> gone, do step 3 again.

> On macOS 14 Sonoma or older: right-click MyEditor in Applications, choose
> **Open**, then click **Open** in the message. macOS 15 removed this shortcut.

> Says "damaged and can't be opened"? Download MyEditor again first. If the
> message stays, open the **Terminal** app, paste this line, press Return, then
> open MyEditor again:
> `xattr -dr com.apple.quarantine "/Applications/MyEditor.app"`

> Apple silicon or Intel? Apple menu > **About This Mac**. A **Chip** line
> (Apple M1 or newer) uses the **arm64** file; a **Processor** line (Intel)
> uses the **x86_64** file.

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

## Updating

When a newer version is out, MyEditor shows a banner at the top of the window.
Click **Update…** to open **Software Update**, which lists the steps for the
way you installed MyEditor. You can also check any time from **Help > Check for
Updates…**.

- **Windows and the AppImage:** click **Update Now**. MyEditor downloads the
  update, offers to save unsaved work, closes, and reopens on the new version.
- **macOS and the `.deb`:** updating takes the same steps as installing. Click
  **Open Update Guide** to follow them in the
  [install guide](https://rinbal.github.io/my_editor/install/), opened for your
  system and in update mode.

**Skip This Version** hides the banner until the next release. Closing the
banner only hides it for now. Your notes and settings carry over across
updates.

---

## Where your files and settings live

Your notes stay wherever you save them. MyEditor keeps its own settings and
cache in `~/.config/my_editor` and `~/.cache/my_editor` on every system.
Removing the app leaves these in place. Delete those two folders for a full
cleanup.
