# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Importer-wide constants.

Values mirror the reference importer where behaviour is shared, so a
given feed produces the same drafts on either side.
"""

from __future__ import annotations

# d-tag prefix applied to newly imported drafts. Prevents collision with
# hand-authored drafts and lets tooling identify imports without parsing
# content. Pre-prefix imports are grandfathered: when a draft with the
# bare (unprefixed) identifier already exists locally, the import reuses
# it so relays replace the old draft instead of duplicating it. See
# ``pipeline.ImportItemsJob._apply_identifier_migration``.
IDENTIFIER_PREFIX: str = "rss-"

# Tag name written on every imported draft's inner event so the UI (and
# any indexer) can identify imports and their origin.
SOURCE_TAG: str = "source"

# Preview scope bounds. The cap keeps a single run from becoming a wall
# of signer approvals; the user can re-import for older items.
DEFAULT_LIMIT: int = 25
MAX_LIMIT: int = 500

# A feed item whose Markdown body (without the source footer) is shorter
# than this is "thin": a teaser rather than an article body.
THIN_CONTENT_CHARS: int = 80

# Relay-friendliness: batches larger than the threshold insert a short
# pause between per-item publishes so write relays and the signer don't
# see one client fire dozens of sign+publish cycles back to back. Small
# batches pay no latency cost.
BATCH_PACE_THRESHOLD: int = 5
BATCH_PACE_MS: int = 250


# --------------------------------------------------------------------- #
# Nostr source resolvers                                                #
# --------------------------------------------------------------------- #

# Relays that index profiles + relay lists (kind 0 / kind 10002), used
# to bootstrap the NIP-65 outbox lookup. purplepag.es specialises in
# exactly this.
NOSTR_INDEXER_RELAYS: tuple = (
    "wss://purplepag.es",
    "wss://relay.nostr.band",
)

# Broad set where long-form content tends to live: the fallback when an
# author advertises no write relays. The app's curated default set
# already covers the majors (primal, damus, nos.lol, yakihonne).
NOSTR_LONGFORM_RELAYS: tuple = (
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nostr-01.yakihonne.com",
)

# Cap on articles / NIPs pulled for one author; mirrors MAX_LIMIT scale
# without letting a prolific author flood the preview query.
NOSTR_MAX_ARTICLES: int = 100

# NostrHub "NIP" (Nostr Implementation Possibility) event kind, and the
# Ditto relays that actually carry those events. They do NOT live on
# the author's kind-10002 outbox, so the resolver queries these
# explicitly (hint relays from an nprofile/naddr merge in ahead).
NOSTRHUB_NIP_KIND: int = 30817
NOSTRHUB_RELAYS: tuple = (
    "wss://relay.ditto.pub",
    "wss://relay.dreamith.to",
)

# NIP-54 wiki article kind (bodies get wikilink/AsciiDoc normalisation).
NOSTR_WIKI_KIND: int = 30818


# --------------------------------------------------------------------- #
# Feed subscriptions                                                    #
# --------------------------------------------------------------------- #

# Kind 30078 (NIP-78 app data) event carrying the encrypted feed list;
# the d-tag namespaces it so it never collides with other apps' data.
SUBSCRIPTIONS_KIND: int = 30078
FEED_LIST_DTAG: str = "myeditor:feed-sources"

# Debounce for publishing subscription changes: rapid add/remove/refresh
# batches into one signed event per window.
SUBSCRIPTIONS_DEBOUNCE_MS: int = 5_000
