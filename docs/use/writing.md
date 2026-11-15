# Writing

MyEditor writes Markdown, the plain text every notes app, blog and Nostr reader understands. You see headings, lists and bold words as they look; the file keeps them as Markdown. Keys are given for a Mac first, then for Windows and Linux. All of them are in [Keyboard shortcuts](shortcuts.md).

## The page

Notes are set in your system's own font at a reading size, across the width of the window with a quiet border on either side. Plain-text and code files use a monospace font across the whole window.

- **View > Paper Mode**, or the page button at the right end of the toolbar, lays the text on a page of about 72 characters instead.
- **View > Appearance**: **Automatic** follows your computer's light or dark appearance; **Light** and **Dark** stay as chosen.
- **View > Background**: lines, dashes, dots or a grid behind the text. **View > Highlight Current Line**, **Show Line Numbers** and **Syntax Highlighting** are there too.
- **View > Zoom In**, **Zoom Out** and **Actual Size** make all text bigger or smaller at once.
- **Settings…** (**⌘,** / **Ctrl+,**) holds the choices you make once: appearance, language, the writing font and its size, the text cursor (Thin, Medium, Thick or Block), and whether the format bar shows over selected text.

The status bar says how long the document is: its words and how long it takes to read, or how many words are selected.

## Format text

The **toolbar** above the document holds the controls used most: Undo and Redo, the paragraph style (Body, Heading 1 to 3), Bold, Italic, Strikethrough, a link, the lists and Quote, and the **+** button, which opens the Insert menu. Hold the pointer over Undo to see what it takes back. The arrow beside Undo and Redo lists the last steps, so you can go back several at once. **View > Show Toolbar** hides it.

**The format bar.** Select words with the pointer and a small bar shows above them, as on an iPad: the paragraph style, Bold, Italic, Strikethrough, Code and Link. It stays while you apply several, and closes when you type or press **Escape**. A selection made with the keyboard shows no bar.

The **Format** menu has everything:

- **Style**: Body (**⌥⌘0** / **Ctrl+0**) and Heading 1 to 3 (**⌥⌘1** to **⌥⌘3** / **Ctrl+1** to **Ctrl+3**). The same heading again makes it Body. **Bigger** and **Smaller** step a paragraph through the same styles.
- **Bold**, **Italic**, **Underline** (HTML documents only, since Markdown has no underline), **Strikethrough** and **Inline Code**.
- **Bulleted List** (**⇧⌘8** / **Ctrl+Shift+8**), **Numbered List** (**⇧⌘7** / **Ctrl+Shift+7**) and **Checklist** (**⇧⌘9** / **Ctrl+Shift+9**). Click a box to tick it, or use **Mark as Checked**. **Tab** and **Shift+Tab** nest an item deeper or bring it back.
- **Quote**, **Increase Indent** and **Decrease Indent**.
- **Color**: red, green, orange, yellow, blue or purple text. Colors stay in HTML and RTF files; Markdown has none, so MyEditor asks before it saves them away.
- **Clear Formatting** takes every style and color off the selection. A link stays a link.

## Links

**Insert > Link…** (**⌘K** / **Ctrl+K**) opens a small panel with the link's words and address. Leave out `https://` and it is added for you; an email address works too. If something cannot be a link, the panel says why. Pasting a web address over selected words links them. **⌘**-click (**Ctrl**-click) opens a link.

## Insert

The **Insert** menu, and the **+** on the toolbar, put structure in at the caret:

- **Image…** and **Table…**. A picture can also be dragged in or pasted. See [Pictures and photo groups](pictures.md).
- **Divider**: a line across the page (`---`).
- **Code Block**: code as it is, in a monospace font on a tinted box, never wrapped. A short list asks for its language, which colors it; **Format > Code Language** changes it later. Inside, **Tab** indents by four spaces, and **⌘Return** / **Ctrl+Return** takes you out of the block.
- **Table…**: choose the columns and rows on a small grid. The first row is the header. **Tab** goes to the next cell and adds a row at the end; **⌘Return** / **Ctrl+Return** leaves the table. **Format > Table** adds and deletes rows and columns.
- **Footnote**: a mark at the caret (`[^1]`) and its note at the end of the document, where the caret goes so you can write it. Hold the pointer over a mark to read its note; **Edit > Go to Note** goes there and back.

**The slash menu.** Type `/` at the start of a word for the same commands without leaving the keyboard: `/he` for the headings, `/list`, `/table`, `/code`. **Up** and **Down** move, **Return** chooses, **Escape** closes it and keeps what you typed. A slash in the middle of a sentence stays a slash.

**Markdown Shortcuts.** If you like typing Markdown, turn on **Edit > Substitutions > Markdown Shortcuts**. Then `# ` makes a heading, `- ` a list, `[ ] ` a checklist item, `> ` a quote, ```` ``` ```` and **Return** a code block, and `**words**` bold words. **Undo** right after a change brings back what you typed.

## Paste

A paste keeps what Markdown can say and nothing else: headings, bold, italic, links, lists, quotes, code blocks, tables and pictures on the web. Fonts, sizes and colors stay behind, so text from Google Docs, Word or a web page looks like the rest of your document. One **Undo** takes the whole paste back.

**Edit > Paste and Match Style** (**⌥⇧⌘V** / **Ctrl+Shift+V**) pastes plain text in the style of the words around it.

## Find and replace

**Edit > Find > Find…** (**⌘F** / **Ctrl+F**) opens the find bar; **Find and Replace…** (**⌥⌘F** / **Ctrl+H**) adds a Replace row with **Replace** and **Replace All**. **Return** and **Shift+Return** step through the matches. The bar's **Options** menu has **Match Case** and **Whole Words**. A replacement takes the style of the words it replaces, and **Replace All** is one step to undo. **Escape** closes the bar.

## Undo

One **Undo** (**⌘Z** / **Ctrl+Z**) takes back a run of typing, as in Pages and Word. A run ends when you pause for two seconds, move the caret, format, paste or save. On a Mac, **Edit > Undo** names what it takes back: "Undo Typing", "Undo Paste", "Undo Bold".

## Spelling

MyEditor checks your spelling with your system's own spell checker, the one your other apps use. Right-click an underlined word for suggestions, **Ignore Spelling** and **Learn Spelling** (**Ignore All** and **Add to Dictionary** on Windows and Linux). **Edit > Spelling > Check Spelling While Typing** turns it off and on. A Mac tells each paragraph's language by itself; on Windows and Linux, **Edit > Spelling** lets you choose it. Code, web addresses and hashtags are not checked.

## Dictation and speech

**Edit > Start Dictation…**, or the microphone at the end of the toolbar, writes what you say:

- **Mac**: your Mac's own dictation.
- **Windows**: Windows Voice Typing, which needs the internet, or dictation on this computer, chosen in **Settings > Dictation**.
- **Linux**: dictation on this computer.

Dictation on this computer works offline. The first time, MyEditor offers to download a speech model (Standard, about 160 MB, or More accurate, about 375 MB); nothing is downloaded until you agree, and your voice never leaves the computer.

**Edit > Speech > Start Speaking** reads the selection, or the whole document, aloud in your system's voice. On Linux it needs `speech-dispatcher`.

## Write, Preview and Split

**View > Write**, **Preview** and **Split** (**⌃⌘1** to **⌃⌘3** / **Ctrl+Shift+1** to **Ctrl+Shift+3**), or the switch at the right end of the toolbar, show a document as you write it, as it reads, or both side by side. The preview follows your typing. In a narrow window, Split shows one side and the switch flips between them.
