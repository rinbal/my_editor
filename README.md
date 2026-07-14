# MyEditor

A clean, minimal note-taking text editor built with Python and PySide6.
Designed for writing lecture notes and personal documents with a distraction-free interface.

<p align="center">
  <img src="assets/texteditor_example.png" width="780" alt="MyEditor screenshot"/>
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
- **Background styles** - lined, dashed, dotted, or grid backgrounds that stay locked to your text baseline; pick one from the `View` menu
- **Paper mode** - center your writing in a page-width column with margins, like a sheet of paper; text re-wraps cleanly at any window size
- **Highlight current line** - a subtle band marks the line you are editing
- **Export formats** - save as `.txt`, `.pdf`, `.md`, `.rtf`
- **Undo / Redo** - full history with header buttons and keyboard shortcuts
- **Syntax highlighting** - automatic language detection by file extension (Python, JavaScript, TypeScript, HTML, CSS, Rust, Go, Java); adapts to dark/light theme; toggleable
- **Recent files** - quick access to the last 10 opened files under `File > Recent Files`
- **Drag and drop** - drag `.txt`, `.md`, `.html` files onto the editor to open them; drag plain text to insert it
- **File change detection** - notifies when an open file is changed externally, with a one-click reload option
- **Crash recovery** - auto-saves every open document in the background; silently restores unsaved work on next launch
- **Session restore** - reopens all previously open files automatically on next launch
- **Nostr publishing** - send the current document straight to Nostr as a short note or a long-form article, signed by your phone via NIP-46. No private key ever lives inside the editor.
- **Private encrypted drafts** - save in-progress work as a NIP-37 draft encrypted to your own Nostr key. The same draft appears on every device you sign in with the same profile.
- **Nostr media library (Blossom)** - upload images, video, and audio to your Blossom servers, browse them in a built-in library, paste / drag-drop / pick to insert into notes, and set article hero images. Mirrored across servers by default and deduplicated by sha256.
- **Import RSS / Atom / JSON feeds as drafts** - paste a blog homepage, article, or feed URL; the editor auto-discovers the feed and mirrors each post as a private NIP-37 draft. Posts whose body lives on Nostr are resolved straight from their kind:30023 long-form event.

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

**"Open with" support:** The `.desktop` file includes a `MimeType` field that registers the editor for common text file types (`.txt`, `.md`, `.html`, `.py`, `.json`, etc.). After installing, you can right-click any text file in your file manager and choose **Open with > MyEditor**.

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

**"Open with" support:** The `Info.plist` includes a `CFBundleDocumentTypes` entry that registers the editor for common text file types. After placing the app in `/Applications/` and launching it once, you can right-click any text file in Finder and choose **Open with > MyEditor**.

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
| `Ctrl+S` | Save (local file, or silent re-save of a draft tab) |
| `Ctrl+Shift+S` | Save As (choose local file or Nostr draft) |
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

The **`View`** menu holds the appearance options: background style (lined, dashed, dotted, grid), paper mode, and highlight current line.

### Nostr

| Shortcut | Action |
|---|---|
| `Ctrl+Shift+P` | Publish current document as a short note (kind 1) |
| `Ctrl+Shift+A` | Publish current document as a long-form article (kind 30023) |
| `Ctrl+Shift+M` | Open the Media Library (Blossom) |
| `Ctrl+Shift+I` | Insert image from the Media Library at the cursor |
| `Ctrl+Shift+D` | Open or close the Drafts panel |
| `Ctrl+Shift+S` | Save current document (chooser: local file or private Nostr draft) |

The **`Nostr`** menu also exposes `Drafts…`, `Connect Signer…`, and `Sign Out Active Profile` for managing identities. The avatar chip at the far right of the header is a one-click profile switcher.

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

Both flows display a `Published from MyEditor` client tag so readers that honour NIP-89 can show which app produced the note.

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

## Private Encrypted Drafts

In-progress work is saved as a **NIP-37 draft**: a kind 31234 event whose body is NIP-44 encrypted to your own Nostr key. Only you can decrypt it, and the encryption happens inside your signer so the editor never holds the plaintext key.

**Cross-device by design.** Drafts live on the same relays you already publish to. Sign in with the same Nostr profile on another device and the editor pulls those drafts straight back into the panel. No cloud account, no separate service.

### How to use it

- **`Ctrl+Shift+D`** opens the Drafts panel on the right.
- **`Ctrl+Shift+S`** asks where to save the current tab: local file or private Nostr draft. The choice can be remembered per tab.
- **`Ctrl+S`** on a draft-bound tab silently re-saves the draft. Same shortcut, no questions, exactly like saving a local file.
- Double-click a row to open the draft in a new tab. Right-click for Publish, Copy event id, or Delete.

### Recovery

If your signer (Amber, nsec.app) times out a decrypt approval or you dismiss the prompt by accident, the row shows up as failed with a one-click retry. Double-click the row or right-click → **Retry decryption** to send the request again. No relay round-trip needed.

### Storage notes

- Drafts are kept on relays for ~90 days then expire (NIP-40). Re-saving extends the window.
- Notes are tagged with a private UUID; articles use a stable slug, so the draft and its eventual published article share the same address.
- Deleting a draft publishes an empty replacement so your other devices see it removed.

---

## Nostr Media (Blossom)

Upload images, video, and audio to your own Blossom servers, browse them in a built-in library, and drop them into notes and articles. All uploads are authorised by a kind 24242 event signed through your existing signer; no separate API key, no third-party account.

### Capabilities

- **Library dialog** (`Ctrl+Shift+M`) - sortable, filterable grid of every blob your pubkey hosts on the configured servers. Per-file: preview, copy URL, download, open in browser, delete (across all mirrors).
- **Upload** four ways: the **Upload** button, drag a file onto the drop zone, **paste** an image with `Ctrl+V` while the grid has focus, or drag an image onto the editor itself.
- **Insert into notes** (`Ctrl+Shift+I`) - pick from the library; alt text is offered inline. Markdown tabs get `![alt](url)`; rich-text tabs embed the cached image. Pasted screenshots auto-upload and auto-insert at the cursor.
- **Hero image for articles** - the Publish Article dialog has a *Cover image* row with a built-in picker; selected images upload to Blossom in the same flow.
- **Mirroring by default** - every upload is mirrored to all configured servers in parallel. Deletes fan out across every server the blob lives on; success on one is treated as success.
- **Dedupe by sha256** - the same file across N servers shows up as one entry; the tile shows a *Nx* badge so you know how widely it's mirrored.
- **Preview lightbox** - 1024 × 720, keyboard nav (← →), shows dimensions, size, mime type, and mirror count alongside Copy URL / Download / Open in browser.

### Default servers

Two operators, chosen for vendor diversity and a published per-file cap:

| URL | Free cap | Notes |
|---|---|---|
| `https://nostr.download` | 100 MiB | Primary (uploads land here first) |
| `https://blossom.primal.net` | ~100 MiB (unpublished) | Mirror |

The list lives in `~/.config/my_editor/blossom_servers.json`. Edit the file to add custom servers; an empty `custom` list falls back to the defaults. (A Settings dialog for in-app management is planned.)

### Upload sizing

The planner checks each configured server's documented per-file limit before sending. If your primary can't take the file but a mirror can, the upload is **rerouted** to the mirror automatically and the status line shows a short note (`blossom.band can't take this file - routing to nostr.download instead`). The hard ceiling is 100 MiB.

### Security model

- **No separate credential.** Every privileged Blossom request carries an `Authorization: Nostr <base64(signed event)>` header where the event is a kind 24242 you signed via NIP-46. Your signer prompts you to approve the first upload / list call in a session.
- **No CORS proxy.** Requests go straight from the desktop app to the Blossom servers.
- **Content-addressed cache.** Thumbnails and previewed bytes are cached at `~/.config/my_editor/blossom_cache/<sha256>` and verified against the hash before use.

---

## Import from RSS / Atom / JSON Feed

Mirror your own blog into private Nostr drafts. Open the Drafts panel with `Ctrl+Shift+D`, switch to the **Feeds** segment, paste a URL, and import. Each surviving item becomes a NIP-37 draft signed by your active profile with title, summary, cover image, hashtags, and original publish date preserved.

### Paste anything

You don't need the feed URL. The editor accepts:

- A feed URL (`https://yourblog.example.com/feed/`)
- The site's homepage or any article URL on the site
- A bare domain like `yourblog.example.com` (the `https://` is added automatically)

Auto-discovery reads `<link rel="alternate" type="application/rss+xml">` from the page head, falls back to the well-known `/feed/` path for WordPress / Ghost / Substack, and explains itself in plain language when no feed is found.

Supports RSS 2.0, Atom, and JSON Feed (WordPress, Ghost, Hugo, Jekyll, Substack, Bear, Mataroa, and friends).

### Nostr-native publishers

Some publishers (Habla, Yakihonne, Pareto, self-hosted Nostr-aware blogs) emit feeds where the body is a teaser and the real article lives on Nostr as a kind:30023 long-form event. When the feed's link is a `nostr:naddr...` URI, or contains a bech32 naddr embedded in an HTTP URL (njump.me, habla.news, yakihonne.com, etc.), the importer fetches the event from your NIP-65 read relays plus the relay hints encoded in the address and uses its prose as the draft body. If the fetch times out or the event is empty, the feed-provided teaser is published instead so the draft always ships.

### Idempotent re-runs

Each item's draft identifier is derived from its feed id, so re-running the same import replaces existing drafts on relays rather than duplicating them. Safe to schedule daily, weekly, or whenever you publish a new post.

---

*built by rinbal*
