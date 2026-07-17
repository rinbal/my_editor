# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Nostr protocol primitives and NIP-46 client for the minimal text editor.

This package implements the subset of Nostr needed to publish kind 1 (short notes)
and kind 30023 (long-form articles) through a remote signer (Amber, nsec.app, etc.)
via NIP-46. No private key ever lives inside the editor process.
"""

# NIP-89-style client identifier. Honoured by readers like Coracle and others
# to display "Published from MyEditor" under the note. The publisher attaches
# ["client", CLIENT_NAME] to every event it builds; remove the tag there to opt
# out per-event.
CLIENT_NAME: str = "MyEditor"


# Default relay set — curated for operator diversity.
#
# Picks 1–5 come from the Buho_go selection (different operators for the first
# four, plus a second YakiHonne for write redundancy). Picks 6–7 are Amber's
# defaults when it generates a bunker URI — including them here means our
# publishing path and the bunker handshake usually share at least one relay,
# improving delivery reliability.
#
# Frozen as a tuple so a future caller can't accidentally mutate the shared
# constant; any override must be a deliberate copy.
DEFAULT_RELAYS: tuple[str, ...] = (
    "wss://relay.primal.net",
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://nostr-01.yakihonne.com",
    "wss://nostr-02.yakihonne.com",
    "wss://nostr.oxtr.dev",
    "wss://theforest.nostr1.com",
)
