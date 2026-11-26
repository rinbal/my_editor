# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a picture's address and description read, without Qt.

photo_group uses these in documents; the Publication Check (nostr/readiness)
uses them on the Markdown that is published, so the "No ALT" chip and the
check never disagree about which picture has no description.
"""

import posixpath

# A picture's address ending in this asks for a picture as wide as the page.
WIDE_SUFFIX = "#wide"


def is_wide(name: str) -> bool:
    """Whether a picture's address asks for the page's width (#wide), as
    STANDUP reads it: the address ends in it, in any case."""
    return bool(name) and name.lower().endswith(WIDE_SUFFIX)


def without_wide(name: str) -> str:
    """The address to load a picture from: without its #wide."""
    return name[:-len(WIDE_SUFFIX)] if is_wide(name) else name


def description_of(alt: str, name: str, stored: str = "") -> str:
    """``alt``, the description of the picture at ``name``, when it
    describes it; "" when it says nothing a person could not see: empty,
    "image" (what the app and Qt's writer put for none), the file's own
    name, with or without its extension (what the app gave a picture it
    added, before), or ``stored``, the name the app keeps for a file of
    its own (an asset's, its file's name when it was added: the address
    "myeditor-asset:…" does not say it)."""
    words = alt.strip()
    file = posixpath.basename(without_wide(name).replace("\\", "/").split("?")[0])
    names = {"", "image"}
    # lower(), not casefold(): STANDUP's twin (publicationCheck.js
    # descriptionOf) has only toLowerCase, and both apps count alike.
    for named in (file, stored):
        names.update((named.lower(), posixpath.splitext(named)[0].lower()))
    return "" if words.lower() in names else words
