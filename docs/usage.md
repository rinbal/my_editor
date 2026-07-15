# Keyboard shortcuts, menus, and save formats

## Keyboard shortcuts

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
| `Escape` | Close find bar and return to editor |

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

The **`Nostr`** menu also exposes `Drafts…`, `Connect Signer…`, and `Sign Out Active Profile` for managing identities. The avatar chip at the far right of the header is a one-click profile switcher. See the [Nostr guide](nostr.md) for the full workflow.

---

## Right-click menu

Right-clicking in the editor opens a context menu with:

- Copy / Cut / Paste
- **Color**: apply one of six text colors (Red, Green, Orange, Yellow, Blue, Purple)
- **Remove Color**: restore default text color
- Bold / Italic / Underline toggles
- **Reset Format**: clear all formatting at once

Right-clicking a **tab** opens a context menu with:

- **Rename**: rename the file on disk and update the tab (greyed out for unsaved files)
- **Delete File**: move the file to system trash with a confirmation dialog (greyed out for unsaved files)

---

## Save formats

| Format | Notes |
|---|---|
| `.txt` | Plain text, no formatting |
| `.pdf` | Print-ready, preserves text colors and formatting |
| `.md` | Markdown |
| `.rtf` | Rich Text Format |
