# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NIP-65, the outbox model: who reads and writes where.

One package owns every answer to "which relays?":

    defaults   the relays MyEditor picks by itself, by role, with the date
               they were last checked (the only place general-purpose
               relay URLs live; the three exceptions are named there)
    policy     the rules, as pure functions (publish routing, private
               storage, reading someone's notes, safe list changes)
    lookup     finding a verified relay list or profile, and telling
               "there is none" apart from "nobody answered"
    directory  RelayDirectory: lookups, caching and the user's own lists
    writer     changing the user's relay list or profile without ever
               overwriting what is already there; setting up new accounts

defaults and policy are plain Python: importing them (or the rules
re-exported here) loads no Qt. RelayDirectory and ask_private_relays are
loaded from directory.py on first use.
"""

from .policy import (  # noqa: F401
    KIND_PROFILE,
    KIND_RELAY_LIST,
    LookupState,
    PublishPlan,
    RelayList,
    bulk_profile_relays,
    dedupe_relays,
    is_public_relay,
    lookup_relays,
    merge_profile_content,
    newest_valid,
    normalize_relay_url,
    outbox_relays,
    parse_relay_list,
    plan_publish,
    private_relays,
    public_relays,
    relays_from,
    relay_list_tags_adding,
    replacement_created_at,
    retry_relays,
    starter_relay_list_tags,
)

_FROM_DIRECTORY = ("RelayDirectory", "ask_private_relays")


def __getattr__(name):
    # PEP 562: the Qt half of the package is imported only when asked for,
    # so the pure rules can be used (and tested) without Qt.
    if name in _FROM_DIRECTORY:
        from . import directory
        return getattr(directory, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
