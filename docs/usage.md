# Keyboard shortcuts, menus, and save formats

## Keyboard shortcuts

### File

| Shortcut | Action |
|---|---|
| `Ctrl+N` | New tab |
| `Ctrl+O` | Open file |
| `Ctrl+S` | Save (local file, or silent re-save of a draft tab) |
| `Ctrl+Shift+S` | Save As (choose local file or Nostr draft) |
| `Ctrl+P` | Print the current tab (`Cmd+P` on macOS) |
| `Ctrl+Shift+K` | Knit R Markdown to HTML (`.Rmd` tabs) |
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

## PDF reading

Opening a `.pdf` (via `Ctrl+O`, drag and drop, double-click from the file
manager, or Recent Files) shows it in the built-in read-only viewer: a slim
toolbar with a contents toggle, a page box, and zoom controls, the document,
and nothing else. Password-protected files prompt for their password. The
viewer remembers the page and zoom you left off at per file, and if the PDF is
regenerated on disk (a LaTeX build, a re-export) it reloads in place at the
same position.

Drag over text to select it and copy with `Ctrl+C`; the selection snaps to
characters like a text editor and pastes cleanly into any tab. Links work the
way you expect: web links open in your browser, internal references (table of
contents entries, "see section 4.2") jump to their page, and the cursor shows
a pointing hand over both.

Navigation follows the muscle memory of readers like SumatraPDF:

| Shortcut | Action |
|---|---|
| `Ctrl+F` | Find in PDF (`Enter` / `Shift+Enter` step through matches) |
| `Ctrl+C` | Copy selected text |
| `Esc` | Clear the selection |
| `Space` / `Shift+Space` | Next / previous screenful |
| `j` / `k` | Scroll down / up |
| `n` / `p` | Next / previous page |
| `g` | Go to page (focuses the toolbar page box) |
| `PageDown` / `PageUp` | Scroll page-wise |
| `Home` / `End` | First / last page |
| `Ctrl+=` / `Ctrl+-` / `Ctrl+wheel` | Zoom in / out |
| `Ctrl+0` | Fit page width |
| `Ctrl+1` | Actual size |
| `Ctrl+2` | Fit whole page |
| `F12` | Toggle the table of contents |
| `F11` | Full screen (whole window; `Ctrl+Cmd+F` on macOS) |

Type a page number into the toolbar's page box and press `Enter` to jump
straight there. **Fit Width** and **Fit Page** in the toolbar switch scaling
modes; searching starts from the page you are reading, not from page one.
Documents with an embedded outline get a **Contents** sidebar (toolbar button
or `F12`); the button stays greyed out when the PDF has no outline.

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

## Printing

`File > Print…` (`Ctrl+P`, `Cmd+P` on macOS) prints the current tab through the
system's print dialog, where you pick the printer, the pages and the number of
copies. A note prints exactly as the `.pdf` export lays it out: the paper size,
orientation and margins from `File > Page Setup…`, images scaled to the page,
and a "Page N of M" footer. A PDF tab prints its own pages, each scaled to fit.
The printer you chose and its options stay selected until you quit.

macOS shows a preview inside its print dialog. On Windows and Linux,
`File > Print Preview…` shows the pages before they print.

---

## Save formats

| Format | Notes |
|---|---|
| `.txt` | Plain text, no formatting |
| `.html` | Clean semantic HTML5; bullets become real lists, images are embedded as data URIs so the single file is shareable; adapts to the reader's light/dark mode |
| `.pdf` | Native PDF export: document metadata, locale-aware page size (A4/Letter), page-number footer, images scaled to the printable width; configure via `File > Page Setup…` |
| `.md` | Markdown |
| `.rtf` | Rich Text Format |
| `.Rmd` | R Markdown: YAML frontmatter plus Pandoc markdown; colors and underline use Pandoc spans, images go into a `<name>_media/` folder next to the file |

---

## R Markdown knitting

`.Rmd` tabs get `File > Knit to HTML` (`Ctrl+Shift+K`) and `File > Knit to PDF`.
Knitting saves the tab, then renders the file through `rmarkdown::render` and
opens the result.

If R, pandoc, or the rmarkdown package are missing, the editor offers to install
them from their official sources (CRAN and the pandoc GitHub releases) into a
private app library. Nothing is downloaded without confirmation, and an existing
system R is always preferred over downloading one. Knit to PDF additionally
needs LaTeX, offered as an optional TinyTeX install (about 100 MB).

`File > R Markdown Toolchain…` shows the status of all components at any time.
