# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The relays MyEditor chooses by itself, and the limits it keeps.

This is the only module that may name general-purpose relays. Everything
else asks nostr/outbox for a set by role, so a relay that goes down or
starts charging is replaced in one place.

Each relay here was checked on QUALIFIED_ON: it answered a read with a
real end-of-stored-events in under three seconds (the bar
tests/smoke_relay_qualify.py sets), and its NIP-11 document asks for no
payment and no login. Relays that failed the check that day
(relay.nostr.band, nostr.oxtr.dev and relay.nsec.app did not connect;
theforest.nostr1.com asks for payment) are deliberately absent. Check
again before changing anything here, and update the date.

Three relay names live elsewhere on purpose: NostrHub's own relays (an
app specific source, nostr/imports/constants.py), the relays used to
meet a signer app (NIP-46 transport, not anyone's home, nostr/bunker.py)
and the association members' relay (a benefit, nostr/einundzwanzig.py).
tests/test_outbox_package.py fails if a relay is named anywhere else.
"""

QUALIFIED_ON = "2026-10-05"

# A brand-new account's relay list (NIP-65 kind 10002). Three relays from
# three operators, each both read and write (no marker): inside the spec's
# advice of two to four per role, and on every major client's defaults.
STARTER_LIST = (
    ("wss://relay.damus.io", ""),
    ("wss://relay.primal.net", ""),
    ("wss://nos.lol", ""),
)

# Where to read or write when someone's relay list is unknown or absent.
FALLBACK_RELAYS = (
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.ditto.pub",
    "wss://relay.nostr.com",
)

# Relays that collect profiles and relay lists from everyone (indexers).
# Asked first when looking someone up, and sent every relay list and
# profile MyEditor publishes, so other clients can find them too.
INDEXER_RELAYS = (
    "wss://purplepag.es",
    "wss://user.kindpag.es",
    "wss://indexer.coracle.social",
)

# Relays that answer NIP-50 search for profiles (kind 0), for finding
# people to mention. Both list 50 in their NIP-11 supported_nips, ask for
# no payment or login, and answered a real profile search on
# QUALIFIED_ON. relay.nostr.band, searched alone before, no longer
# connects.
SEARCH_RELAYS = (
    "wss://relay.ditto.pub",
    "wss://search.nos.today",
)

# Limits.
WRITE_CAP = 6               # own write relays used per publish (spec: 2-4; tolerate more)
MIN_WRITE_TARGETS = 2       # top up from FALLBACK_RELAYS below this
INBOX_PER_MENTION = 2       # read relays per person a note mentions (NIP-65 says all)
MENTION_LOOKUP_CAP = 20     # mentions whose relay lists are looked up
INBOX_TOTAL_CAP = 10        # inbox relays per publish, all mentions together, shared in turn
LOOKUP_CAP = 8              # relays asked for someone's relay list, an indexer always among them
HINT_CAP = 3                # relays from hints (tags, addresses) asked for one person
PRIVATE_CAP = 10            # relays for drafts and other private records
DIRECTORY_CAP = 500         # other people's relay lists kept in memory

# How long a looked-up relay list is trusted before it is refreshed.
TTL_FOUND_S = 30 * 60
TTL_ABSENT_S = 3 * 60
TTL_UNKNOWN_S = 30

# A list counts as absent only when at least this many relays answered
# "nothing stored", one of them an indexer. Anything less is "unknown".
ABSENT_QUORUM = 2

# A relay list or profile to change that is dated further ahead than this
# is refused (the change would be dated after it): a day covers any clock
# that is merely off.
MAX_BASE_AHEAD_S = 24 * 60 * 60
