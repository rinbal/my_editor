# 3. Photo groups follow STANDUP's layout

Status: accepted, 2026-11-15 ([#61](https://github.com/rinbal/my_editor/pull/61), part of STANDUP issue 115).

## Context

Writers put several pictures in a row. Shown one under another they take a lot of room; cropped into a fixed grid they lose what matters. STANDUP (the EINUNDZWANZIG publishing platform) and its reader Discover already lay such pictures out as a calm mosaic in which every picture keeps its shape. An article written in MyEditor should look the same there as it did while it was written.

## Decision

Pictures that follow each other, with nothing but empty lines between them, form a photo group. MyEditor shows it with STANDUP's own layout engine, in the editor, the preview and every export. A picture marked `#wide` gets a row of its own across the whole width. The Markdown stays what the author wrote: one picture paragraph each, with its caption, so every other app shows the pictures one under another.

The engine and page parts are taken over from STANDUP and keep STANDUP's MIT licence: `photo_layout.py`, `web/photo_layout.js`, `web/photo_viewer.js`, `web/photo_groups.js`, `web/photo_groups.css`, `web/photo-group-messages.json` and their test data.

## Consequences

- Those files must stay byte for byte as STANDUP has them: tests compare their hashes, and `.gitattributes` keeps them with LF line ends. Change them in STANDUP first.
- Cropping makes a new file; a picture's own file is never changed.
- How a group knits in R Markdown, and how it prints, is described in [Save, export and print](../use/save-export-print.md).
