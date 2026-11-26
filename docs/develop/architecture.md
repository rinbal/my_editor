# How MyEditor is built

MyEditor is a Python desktop app on Qt 6 (PySide6). It runs from source on any system with Python; the installers bundle it with PyInstaller ([packaging/README.md](../../packaging/README.md)). CI tests it on Windows, macOS and Linux with Python 3.12.

`main.py` starts the app and opens `MainWindow` (`main_window.py`), which builds the menus, the tabs and the side panels. Most modules hold one idea each and say so in their first docstring line; read that line before you change a file.

## One home for each rule

Several rules live in exactly one module, so that two places can never disagree. Change the rule there, not around it.

| Rule | Module |
|---|---|
| Every command: its menu, title, shortcut, slash-menu entry, and its line in Help > Keyboard Shortcuts | `commands.py` (the commands themselves are declared in `main_window.py`) |
| Every Markdown the app writes: files, drafts, published articles | `markdown_writer.py` |
| Every file write: atomic, keeps permissions, Finder tags and line endings | `atomic_file.py` |
| Every text a person sees, in their language | `i18n.py` and `locale/` ([translating.md](translating.md)) |
| Whether Nostr is in use | `nostr/state.py` ([decision 2](../decisions/0002-nostr-is-opt-in.md)) |
| How a photo group is laid out | `photo_layout.py`, shared with STANDUP ([decision 3](../decisions/0003-photo-groups-follow-standup.md)) |
| Where the app keeps its own files: `~/.config/my_editor` and `~/.cache/my_editor` | `user_dirs.py` |
| Which addresses the app may fetch or open | `url_safety.py` |

## Where things are

| Area | Main modules |
|---|---|
| Writing | `editor.py`, `rich_text.py`, `paste.py`, `markdown_shortcuts.py`, `slash_menu.py`, `format_toolbar.py`, `selection_bar.py`, `find_replace.py`, `highlighter.py`, `front_matter.py` |
| Pictures and photo groups | `photo_group*.py`, `photo_layout.py`, `photo_page.py`, `picture_crop.py`, `web/` |
| Preview, export and print | `preview_pane.py`, `export_html.py`, `export_pdf.py`, `printing.py`, `rmarkdown.py`, `rmd_toolchain.py` |
| Folders and files | `library_set.py`, `library_sidebar.py`, `library_window.py`, `file_list.py`, `folder_library.py`, `link_rewrite.py`, `quick_look.py` |
| Viewers | `pdf_viewer.py`, `viewer_tabs.py`, `media_tabs.py`, `office_preview.py` |
| Spreadsheets | `sheet_tab.py`, `sheet_io.py`, `sheet_formulas.py`, `formula_names.py` |
| Keeping work safe | `recovery.py` (crash backups), `locked_files.py` (password lock), `workspace.py` (tabs across an update) |
| Speech and spelling | `dictation/`, `speech.py`, `spelling/` ([spelling.md](spelling.md)) |
| Updates | `update_check.py`, `update_dialog.py`, `update_flow.py`, `updater.py`, `verified_download.py` ([install-guide.md](install-guide.md)) |
| Nostr | `nostr/` and `nostr/ui/` ([nostr.md](nostr.md)) |
| Membership service | `sidecar/`, a small server that holds the association's API key ([sidecar/README.md](../../sidecar/README.md)) |

## Look and theme

`theme.py` holds the app's palette for light and dark, and `theme.AppStyle` the Fusion style with dialog buttons in the order Apple's guidelines give on every system (Cancel, then the default action last). Alerts follow Apple's layout everywhere (`alerts.py`).

Windows that act for a Nostr account wear Night Violet ([decision 1](../decisions/0001-night-violet.md)). `nostr/ui/appearance.py` resolves one set of colour roles for palettes, stylesheets and painters. Ownership is explicit: a container that belongs to the Nostr workflow asks for its surface, its content keeps the neutral look, and a detached window joins appearance changes through `bind`. `nostr/ui/native_appearance.py` reads the system's increased contrast, reduced transparency and Reduce Motion settings. Nothing in these modules touches accounts, documents or network traffic.

## Tests

The tests live in `tests/` and run with pytest. `tests/conftest.py` gives every run a home folder of its own, so no test reads or writes your settings, and fails the run if your own `~/.config/my_editor` changed. `tests/window_fixture.py` builds a real main window inside a test. [CONTRIBUTING.md](../../CONTRIBUTING.md#run-the-tests-that-matter) says how to run only the tests your change needs.

## Releases

Release notes are written per version in [docs/releases/](../releases/README.md); the release workflow uses `docs/releases/v<version>.md` as the GitHub release text. The steps are in [release-process.md](release-process.md). A beta tagged on `dev` is published as a pre-release, with a list of changes GitHub writes: see [Beta releases](../../packaging/README.md#beta-releases).
