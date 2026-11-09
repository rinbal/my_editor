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

# MyEditor's own page and icon, as a signer app shows them where the person
# approves a connection (NIP-46 ``url`` and ``image``). The icon is the one
# packaging/site/build_site.py publishes with the install guide.
CLIENT_URL: str = "https://rinbal.github.io/my_editor/"
CLIENT_IMAGE: str = "https://rinbal.github.io/my_editor/install/icon.png"

# Which relays to use is nostr/outbox's answer, by role; the relays
# MyEditor picks by itself are in nostr/outbox/defaults.py.
