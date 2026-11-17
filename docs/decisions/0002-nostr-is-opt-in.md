# 2. Nostr is opt-in

Status: accepted, 2026-10-05.

## Context

MyEditor began as a calm local text editor and grew a Nostr publishing client. Most people who open it want to write and read. Nostr buttons, panels and prompts in front of them would make the app busier and harder to understand, and would suggest that an account is needed to use it.

## Decision

The editor shows nothing of Nostr until the person chooses it. The Nostr menu is the one way in. Once an account is in use (connected through a signer app, or kept on this computer), the Nostr features appear where they help: the Insert menu's Nostr items, the slash menu, the Drafts and Published panels, Article Details and the account button at the end of the tab row. Signing out takes them away again.

Every surface asks one object whether Nostr is in use (`nostr/state.py`, `NostrState.active`), instead of checking accounts its own way.

## Consequences

- Docs and the README present MyEditor as a writing and reading app first; Nostr publishing is one optional benefit, documented in its own section.
- A new Nostr feature must follow `NostrState`: hidden or dimmed without an account, never in the way of the local editor.
- Help > Keyboard Shortcuts lists the Nostr shortcuts only while an account is in use.
