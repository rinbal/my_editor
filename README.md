# minimal texteditor

A clean, minimal note-taking text editor built with Python and PySide6.
Designed for writing lecture notes and personal documents with a distraction-free interface.

<p align="center">
  <img src="assets/texteditor_example.png" width="780" alt="minimal texteditor screenshot"/>
</p>

---

## Features

- **Dark / Light theme** - toggle anytime, text colors adapt automatically
- **Multiple tabs** - work on several documents at once; drag to reorder
- **Rich text formatting** - bold, italic, underline, text colors; **B / I / U buttons** in the header show and toggle the active state at the cursor
- **Bullet lists** - smart bullet behavior with Tab indentation
- **Find bar** - search with match counter and previous/next navigation
- **Line numbers** - optional gutter on the left
- **Line counter** - current line and total lines always visible in the bottom right corner (`Ln X / Y`)
- **Export formats** - save as `.txt`, `.pdf`, `.md`, `.rtf`
- **Undo / Redo** - full history with header buttons and keyboard shortcuts
- **Syntax highlighting** - automatic language detection by file extension (Python, JavaScript, TypeScript, HTML, CSS, Rust, Go, Java); adapts to dark/light theme; toggleable
- **Recent files** - quick access to the last 10 opened files under `File > Recent Files`
- **Drag and drop** - drag `.txt`, `.md`, `.html` files onto the editor to open them; drag plain text to insert it
- **File change detection** - notifies when an open file is changed externally, with a one-click reload option
- **Crash recovery** - auto-saves every open document in the background; silently restores unsaved work on next launch
- **Session restore** - reopens all previously open files automatically on next launch
- **Nostr publishing** - send the current document straight to Nostr as a short note or a long-form article, signed by your phone via NIP-46. No private key ever lives inside the editor.

---

  > [!NOTE]                                                                                                                                              
  > Developed and tested on Linux. macOS and Windows should work but may show minor visual differences.

## Installation

**Requirements:** Python 3.10+

```bash
git clone https://github.com/rinbal/my_editor.git
cd my_editor
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Running

```bash
source .venv/bin/activate
python main.py
```

You can also open a file directly:

```bash
python main.py /path/to/file.txt
```

---

## App Shortcuts

Three launcher templates are included, one per platform:
- `my-editor.desktop.example` - Linux
- `my-editor.app.example/` - macOS
- `my-editor.bat.example` - Windows

### Linux - Desktop Shortcut

The file `my-editor.desktop.example` is a template to register the editor as an application in your Linux desktop environment (GNOME, KDE, etc.).

**1. Find the full path to the project folder:**
```bash
pwd
```
This prints something like `/home/yourname/my_editor` - copy that path.

**2. Copy the template:**
```bash
cp my-editor.desktop.example my-editor.desktop
```

**3. Open `my-editor.desktop` and replace both `/path/to/my_editor` entries with your actual path.**

Example with path `/home/yourname/my_editor`:
```
Exec=/home/yourname/my_editor/.venv/bin/python /home/yourname/my_editor/main.py %F
Path=/home/yourname/my_editor
```

**4. Install the shortcut:**
```bash
cp my-editor.desktop ~/.local/share/applications/
update-desktop-database ~/.local/share/applications/
```

The editor will now appear in your app launcher and can be pinned to the dock.

**"Open with" support:** The `.desktop` file includes a `MimeType` field that registers the editor for common text file types (`.txt`, `.md`, `.html`, `.py`, `.json`, etc.). After installing, you can right-click any text file in your file manager and choose **Open with > minimal texteditor**.

---

### macOS - App Bundle

The folder `my-editor.app.example/` is a template for a native macOS `.app` bundle. On macOS, any folder named `Something.app` with the right internal structure is treated as a clickable application - no installation tool required.

**Structure of the bundle:**
```
my-editor.app/
└── Contents/
    ├── Info.plist          ← app metadata
    ├── MacOS/
    │   └── my-editor       ← launcher shell script (must be executable)
    └── Resources/          ← optional: place your icon (.icns) here
```

**1. Find the full path to the project folder:**
```bash
pwd
```
This prints something like `/Users/yourname/my_editor` - copy that path.

**2. Copy the template bundle:**
```bash
cp -r my-editor.app.example my-editor.app
```

**3. Open `my-editor.app/Contents/MacOS/my-editor` in a text editor and replace the path:**

Example with path `/Users/yourname/my_editor`:
```bash
cd /Users/yourname/my_editor
.venv/bin/python main.py
```

**4. Make the launcher script executable:**
```bash
chmod +x my-editor.app/Contents/MacOS/my-editor
```

**5. Move the app to Applications (optional but recommended):**
```bash
mv my-editor.app /Applications/
```

You can now double-click `my-editor.app` in Finder to launch the editor, or drag it to the Dock to pin it.

**"Open with" support:** The `Info.plist` includes a `CFBundleDocumentTypes` entry that registers the editor for common text file types. After placing the app in `/Applications/` and launching it once, you can right-click any text file in Finder and choose **Open with > minimal texteditor**.

> **Note:** macOS may show a security warning the first time you open the app since it is not from the App Store. To bypass it: right-click the app → **Open** → confirm in the dialog. You only need to do this once.

> **Icon:** By default the app shows a generic icon. To use a custom icon, place a `.icns` file in `my-editor.app/Contents/Resources/` and add the following to `Info.plist` inside the `<dict>` block:
> ```xml
> <key>CFBundleIconFile</key>
> <string>your-icon-name</string>
> ```
> To convert a PNG to `.icns` on macOS, see `iconutil` (built into macOS).

---

### Windows - Batch Script

The file `my-editor.bat.example` is a template for a double-clickable launcher script on Windows.

**1. Find the full path to the project folder:**

Open the project folder in File Explorer, click the address bar, and copy the path. It will look something like `C:\Users\yourname\my_editor`.

**2. Copy the template:**
```
copy my-editor.bat.example my-editor.bat
```

**3. Open `my-editor.bat` in a text editor and replace the path with your actual path.**

Example with path `C:\Users\yourname\my_editor`:
```bat
cd C:\Users\yourname\my_editor
.venv\Scripts\python.exe main.py
```

**4. Double-click `my-editor.bat` to launch the editor.**

**"Open with" support:** Windows cannot register `.bat` files in the "Open with" menu automatically. However, if you manually associate a file type with the editor once (right-click a file → **Open with > Choose another app** → browse to the `.bat` file), Windows will remember the choice and the file will open correctly because the `.bat` passes its arguments to `main.py`.

**Optional - Pin to taskbar or Start Menu:**
- **Taskbar:** Right-click `my-editor.bat` → **Pin to taskbar**
- **Start Menu:** Place a shortcut to the `.bat` file in:
  ```
  %APPDATA%\Microsoft\Windows\Start Menu\Programs\
  ```

> **Note:** A terminal window will briefly appear on launch - this is normal for `.bat` files on Windows.

> **Icon:** `.bat` files cannot carry a custom icon directly. To use one, create a Windows shortcut (`.lnk`) to the `.bat` file, then right-click the shortcut → **Properties** → **Change Icon** and select any `.ico` file.

---

## Keyboard Shortcuts

### File

| Shortcut | Action |
|---|---|
| `Ctrl+N` | New tab |
| `Ctrl+O` | Open file |
| `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save As |
| `Ctrl+W` | Close tab |
| `Ctrl+Q` | Quit |

### Formatting

| Shortcut | Action |
|---|---|
| `Ctrl+B` | Bold |
| `Ctrl+I` | Italic |
| `Ctrl+U` | Underline |
| `Ctrl+D` | Reset all formatting |

The **B**, **I**, and **U** buttons in the header bar mirror these shortcuts and highlight orange when the format is active at the cursor position.

### Undo / Redo

| Shortcut | Action |
|---|---|
| `Ctrl+Z` | Undo |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo |

### Search

| Shortcut | Action |
|---|---|
| `Ctrl+F` | Open find bar |
| `Enter` | Next match (while find bar is open) |
| `Shift+Enter` | Previous match (while find bar is open) |
| `F3` | Find next |
| `Shift+F3` | Find previous |
| `Escape` | Close find bar & return to editor |

### Editor

| Shortcut | Action |
|---|---|
| `Tab` | Indent / create bullet |
| `Shift+Tab` | Outdent / remove bullet indent |
| `Enter` | New line (continues bullet if active) |
| `Enter` (on empty bullet) | Exit bullet mode |
| `Ctrl+Shift+L` | Toggle line numbers |
| `Ctrl+Shift+T` | Toggle dark / light theme |
| `Ctrl+Shift+H` | Toggle syntax highlighting |

### Nostr

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+P` | Publish current document as a short note (kind 1) |
| `Ctrl+Shift+A` | Publish current document as a long-form article (kind 30023) |

The **`Nostr`** menu also exposes `Connect Signer…` and `Sign Out Active Profile` for managing identities. The avatar chip at the far right of the header is a one-click profile switcher.

---

## Right-Click Menu

Right-clicking in the editor opens a context menu with:

- Copy / Cut / Paste
- **Color** - apply one of six text colors (Red, Green, Orange, Yellow, Blue, Purple)
- **Remove Color** - restore default text color
- Bold / Italic / Underline toggles
- **Reset Format** - clear all formatting at once

Right-clicking a **tab** opens a context menu with:

- **Rename** - rename the file on disk and update the tab (greyed out for unsaved files)
- **Delete File** - move the file to system trash with a confirmation dialog (greyed out for unsaved files)

---

## Save Formats

| Format | Notes |
|---|---|
| `.txt` | Plain text, no formatting |
| `.pdf` | Print-ready, preserves text colors and formatting |
| `.md` | Markdown |
| `.rtf` | Rich Text Format |

---

## Nostr Publishing

Write in the editor, hit publish, approve on your phone. The document goes out as a Nostr event. Your private key stays in your signer (Amber, nsec.app, nsec.bunker, etc.) and never enters this app.

### What you can publish

- **Short notes** (kind 1, `Ctrl+Shift+P`): the editor's content as plain text. Formatting is stripped on publish.
- **Long-form articles** (NIP-23 kind 30023, `Ctrl+Shift+A`): Markdown body with title, summary, slug (the `d`-tag identifier), cover image, and hashtags. Re-publishing with the same slug replaces the previous version, so an article stays addressable as one `naddr1…` link across edits.

Both flows display a `Published from My-Editor` client tag so readers that honour NIP-89 can show which app produced the note.

### Connecting a signer

Three pairing flows in one dialog (`Nostr → Connect Signer…`):

1. **Paste URI**: paste a `bunker://` URL generated by your signer.
2. **Scan QR**: the editor shows a `nostrconnect://` QR code that any NIP-46 signer can scan. Used for cross-device pairing.
3. **Manual**: supply the bunker pubkey, relay list, and optional secret separately.

Connected profiles are saved to `~/.config/my_editor/nostr_profiles.json` with `chmod 600`. The local channel keypair lives there too; your real `nsec` does not.

### Multiple identities

The avatar chip at the far right of the header is a profile switcher. Add as many profiles as you want, switch with a single click, and the change takes effect on the next publish. Switching mid-write is fine; the active profile is finalised only when you actually press **Publish** inside the publish dialog (which also has its own inline switcher).

Avatars and display names are pulled from each profile's kind 0 metadata in the background; until they land the chip shows colored initials.

### Tagging people (mentions)

Inside both publish dialogs there's a **Mentions** chip row. Clicking **+ add person** opens a picker that searches:

- your **NIP-02 contact list** first (instant, offline after the first fetch), and
- **NIP-50 search relays** (`relay.nostr.band`) for anyone you don't already follow.

Picked profiles become inline pills and are emitted on publish as `["p", <pubkey>, <relay-hint>]` tags plus a `nostr:nprofile1…` URI appended to the body, so mention rendering works in every client. Inline `nostr:n…` URIs you paste yourself are also picked up and deduplicated automatically.

### Relays and routing

Publishing follows the **NIP-65 outbox model**:

- Always include a curated base set (Primal, Damus, nos.lol, two YakiHonne relays, `nostr.oxtr.dev`, `theforest.nostr1.com`).
- Union with the **user's own write relays** from their kind 10002 list, fetched once per profile and cached for 30 minutes.
- Deduplicate and cap at 10 relays per publish.

The publisher uses eager-first-accept semantics: as soon as one relay acknowledges the event, the dialog shows the result; remaining relays continue in the background and the final count lands in the status bar (e.g. `Published to Nostr: 6/7 relays · note1xxxxx…`).

### Security model

- **No private key in the editor.** All signing goes through NIP-46 over NIP-44 v2 encryption. Every `sign_event` call surfaces an approval prompt in your signer.
- **Connection spoof protection.** The `nostrconnect://` flow generates a one-time secret that the editor verifies against the signer's response before completing the handshake.
- **Profile file permissions.** The on-disk profile store is restricted to the owner. The local channel keypair stored there only authorizes the existing bunker session; it cannot sign anything itself.

---

*built by rinbal*
