# minimal texteditor

A clean, minimal note-taking text editor built with Python and PySide6.
Designed for writing lecture notes and personal documents with a distraction-free interface.

---

## Features

- **Dark / Light theme** - toggle anytime, text colors adapt automatically
- **Multiple tabs** - work on several documents at once
- **Rich text formatting** - bold, italic, underline, text colors
- **Bullet lists** - smart bullet behavior with Tab indentation
- **Find bar** - search with match counter and previous/next navigation
- **Line numbers** - optional gutter on the left
- **Export formats** - save as `.txt`, `.pdf`, `.md`, `.rtf`
- **Undo / Redo** - full history with header buttons and keyboard shortcuts

---

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

## Desktop Shortcut (Linux)

A template `.desktop` file is included so you can launch the editor with a single click from your app launcher or dock.

**1. Find out the full path to the project folder:**
```bash
pwd
```
This prints something like `/home/yourname/my_editor` - copy that path.

**2. Copy the template file:**
```bash
cp my-editor.desktop.example my-editor.desktop
```

**3. Open `my-editor.desktop` in a text editor and replace both `/path/to/my_editor` entries with your actual path from step 1.**

For example, if your path is `/home/yourname/my_editor`, the relevant lines should look like:
```
Exec=/home/yourname/my_editor/.venv/bin/python /home/yourname/my_editor/main.py
Path=/home/yourname/my_editor
```

**4. Install the shortcut:**
```bash
cp my-editor.desktop ~/.local/share/applications/
```

The editor will now appear in your app launcher and can be pinned to the dock.

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

### Undo / Redo

| Shortcut | Action |
|---|---|
| `Ctrl+Z` | Undo |
| `Ctrl+Y` / `Ctrl+Shift+Z` | Redo |

### Search

| Shortcut | Action |
|---|---|
| `Ctrl+F` | Open find bar |
| `F3` | Find next |
| `Shift+F3` | Find previous |
| `Escape` | Close find bar |

### Editor

| Shortcut | Action |
|---|---|
| `Tab` | Indent / create bullet |
| `Shift+Tab` | Outdent / remove bullet indent |
| `Enter` | New line (continues bullet if active) |
| `Enter` (on empty bullet) | Exit bullet mode |
| `Ctrl+Shift+L` | Toggle line numbers |
| `Ctrl+Shift+T` | Toggle dark / light theme |

---

## Right-Click Menu

Right-clicking in the editor opens a context menu with:

- Copy / Cut / Paste
- **Color** - apply one of six text colors (Red, Green, Orange, Yellow, Blue, Purple)
- **Remove Color** - restore default text color
- Bold / Italic / Underline toggles
- **Reset Format** - clear all formatting at once

---

## Save Formats

| Format | Notes |
|---|---|
| `.txt` | Plain text, no formatting |
| `.pdf` | Print-ready, preserves text colors and formatting |
| `.md` | Markdown |
| `.rtf` | Rich Text Format |

---

*created by rinbal*
