# 4. A co-author is credit; sharing zaps is a separate choice

Status: accepted, 2026-10-13.

## Context

Articles can name co-authors. On Nostr, readers can also send zaps (small Lightning payments) to an article, and an article can say how zaps are split. Treating every co-author as someone who automatically shares the money would surprise writers who only wanted to give credit.

## Decision

Credit, zaps optional. Naming a co-author credits them as an author and nothing more. Sharing zaps is a second, explicit choice: **Share zaps equally with co-authors** in Article Details. Then each person, the writer included, gets an equal share. A co-author whose profile has no Lightning address cannot receive zaps and gets no share; the app says so under the option.

## Consequences

- A co-author is tagged the way STANDUP writes and reads authorship, so both apps agree who wrote an article. See [How MyEditor speaks Nostr](../develop/nostr.md#co-authors-and-zap-splits).
- Zap splits another app set keep their weights until the writer changes them in Article Details.
