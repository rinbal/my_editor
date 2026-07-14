# MyEditor v3.0

## Highlights

- **Nostr publishing** - send the current document straight to Nostr as a short note or a long-form article, signed on your phone via NIP-46. Your private key never enters the editor.
- **Private encrypted drafts** - save work in progress as a NIP-37 draft encrypted to your own key. Sign in on another device with the same profile and your drafts follow you.
- **Nostr media library (Blossom)** - upload images, video, and audio to your own Blossom servers, browse them in a built-in library, and drop them into notes and articles. Mirrored across servers and deduplicated by hash.
- **Import RSS / Atom / JSON feeds** - paste a blog homepage, article, or feed URL and mirror each post as a private draft, with title, summary, cover image, and date preserved.
- **Background styles and paper mode** - lined, dashed, dotted, or grid backgrounds that stay locked to your text baseline, plus a paper mode that centers your writing in a page-width column with margins. All in the new `View` menu.
- **One-click installers** - native downloads for Windows, macOS, and Linux. No Python setup, just download and open.
- **Multiple Nostr identities** - a profile switcher in the header. Add as many profiles as you want and switch with a single click.
- **Welcome screen** - a friendly first-run tab that introduces the app.
- **Update notifications** - a quiet banner tells you when a newer version is available.
- **Open with integration** - register the editor as the handler for common text files and open them straight from your file manager.
- **Shortcuts reference** - a Help window listing every keyboard shortcut.

## Technical updates & fixes

- Smart line wrapping: text re-wraps correctly when toggling paper mode or resizing the window, in both directions, with no leftover layout and no horizontal scrollbar.
- NIP-65 outbox relay routing with per-profile write-relay caching, eager first-accept publishing, and a cap of 10 relays per publish.
- NIP-44 v2 encryption throughout the Nostr and drafts flows. Every signing call goes through your NIP-46 signer with an approval prompt.
- Large automated test suite added: NIP-44 vectors, drafts, outbox, RSS discovery and parsing, Blossom auth and planning, profiles, mentions, and bech32.
- Cross-platform packaging pipeline: a shared PyInstaller spec, per-OS wrappers, and a GitHub Actions build that attaches installers to each release.
- Settings now persist to disk (theme and view options) under `~/.config/my_editor`.
- Linux AppStream metadata added for software-center listings.

## Contributors

Thanks to everyone who shipped this release:

- [@rinbal](https://github.com/rinbal) - maintainer. Open with integration, the new `View` menu (background styles, paper mode, highlight current line), welcome screen, settings persistence, and update checker.
- [@DoktorShift](https://github.com/DoktorShift) - Nostr publishing ([#22](https://github.com/rinbal/my_editor/pull/22)), private encrypted drafts ([#26](https://github.com/rinbal/my_editor/pull/26)), RSS and Blossom import ([#27](https://github.com/rinbal/my_editor/pull/27)), and one-click installers ([#29](https://github.com/rinbal/my_editor/pull/29)).

## Community

- Nostr: https://nostr-ecosystem.netlify.app/join/g/groups.0xchat.com/my-editor-public-talk?n=My+Editor+-+Public+Talk&a=A+community+for+users%2C+contributors%2C+and+anyone+interested+in+a+clean%2C+distraction-free+note-taking+editor+built+with+Python+and+PySide6.%0A%0AS&p=https%3A%2F%2Fblossom.primal.net%2F6173a8dc06038a1d67d6149755166fb48d74d92d41ef6af8b6cd863489eb3095

## Download

- Github: https://github.com/rinbal/my_editor
