# Save, export and print

**File > Save** (**⌘S** / **Ctrl+S**) saves a document where it is. **File > Save As…** (**⇧⌘S** / **Ctrl+Shift+S**) saves it under another name or in another format. **File > Revert to Saved…** goes back to the last saved version, after asking.

## Formats

Choose the format in the Save As window, by its file type.

| Format | What it keeps |
|---|---|
| Markdown (`.md`) | Headings, bold, italic, strikethrough, code, links, lists, checklists, quotes, code blocks, tables, footnotes and photo groups. Underline and colors have no Markdown, so MyEditor asks first when a document has them: save as HTML or RTF to keep them, or **Save Without Colors**, which also takes them out of the document (**Undo** brings them back). Pictures go into a folder beside the file. This is the same Markdown an article publishes. |
| HTML (`.html`) | Everything, in one file you can send: pictures are inside it, photo groups keep their mosaic, and the page follows the reader's light or dark appearance. |
| PDF (`.pdf`) | Pages as **File > Page Setup…** sets them: paper size (A4 or Letter, from your region), orientation and margins, pictures scaled to the page, and a page number in the footer. |
| Rich Text (`.rtf`) | Formatted text for word processors, colors and underline too. |
| Plain text (`.txt`) | The words only, without formatting. |
| R Markdown (`.Rmd`) | Markdown with a YAML header for R. Colors, highlights and underline are kept as Pandoc spans; pictures go into a folder beside the file. |

Files MyEditor saves are written safely: a crash while saving never leaves half a file, and a file keeps its permissions, Finder tags and line endings.

## Print

**File > Print…** (**⌘P** / **Ctrl+P**) prints the current tab through your system's print window, where you choose the printer, the pages and the copies. A note prints exactly as its PDF would look, with the paper, orientation and margins of **File > Page Setup…** (**⇧⌘P** on a Mac) and "Page N of M" at the foot of each page. A PDF prints its own pages, each scaled to the paper.

A Mac shows a preview in its print window. On Windows and Linux, **File > Print Preview…** (**Ctrl+Shift+O**) shows the pages before they print.

## R Markdown knitting

R Markdown tabs (`.Rmd`) get **File > R Markdown > Knit to HTML** (**⇧⌘K** / **Ctrl+Shift+K**) and **Knit to PDF**. Knitting saves the tab, renders the file with R's `rmarkdown::render`, and opens the result.

Pictures that follow each other (a [photo group](pictures.md)) are saved to R Markdown as one `::: {.photo-group}` block, a picture or a caption on each line. Knit to HTML lays them out side by side in rows, as a document saved as HTML shows them. In a PDF or Word document knit from the file, here or in RStudio, they stand one under another, each picture with its caption under it, as other apps show them.

If R, pandoc or the rmarkdown package is missing, MyEditor offers to install it from its official source (CRAN and pandoc's GitHub releases) into a folder of its own. Nothing is downloaded without your OK, and an R already on your computer is always used first. Knit to PDF also needs LaTeX, offered as an optional TinyTeX install of about 100 MB.

**File > R Markdown > R Markdown Toolchain…** shows what is installed at any time.
